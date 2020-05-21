#!/usr/bin/python
# Copyright 2017 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""This script helps to generate code coverage report.

  It uses Clang Source-based Code Coverage -
  https://clang.llvm.org/docs/SourceBasedCodeCoverage.html

  In order to generate code coverage report, you need to first add
  "use_clang_coverage=true" and "is_component_build=false" GN flags to args.gn
  file in your build output directory (e.g. out/coverage).

  * Example usage:

  gn gen out/coverage \\
      --args="use_clang_coverage=true is_component_build=false\\
              is_debug=false dcheck_always_on=true"
  gclient runhooks
  python tools/code_coverage/coverage.py crypto_unittests url_unittests \\
      -b out/coverage -o out/report -c 'out/coverage/crypto_unittests' \\
      -c 'out/coverage/url_unittests --gtest_filter=URLParser.PathURL' \\
      -f url/ -f crypto/

  The command above builds crypto_unittests and url_unittests targets and then
  runs them with specified command line arguments. For url_unittests, it only
  runs the test URLParser.PathURL. The coverage report is filtered to include
  only files and sub-directories under url/ and crypto/ directories.

  If you want to run tests that try to draw to the screen but don't have a
  display connected, you can run tests in headless mode with xvfb.

  * Sample flow for running a test target with xvfb (e.g. unit_tests):

  python tools/code_coverage/coverage.py unit_tests -b out/coverage \\
      -o out/report -c 'python testing/xvfb.py out/coverage/unit_tests'

  If you are building a fuzz target, you need to add "use_libfuzzer=true" GN
  flag as well.

  * Sample workflow for a fuzz target (e.g. pdfium_fuzzer):

  python tools/code_coverage/coverage.py pdfium_fuzzer \\
      -b out/coverage -o out/report \\
      -c 'out/coverage/pdfium_fuzzer -runs=0 <corpus_dir>' \\
      -f third_party/pdfium

  where:
    <corpus_dir> - directory containing samples files for this format.

  To learn more about generating code coverage reports for fuzz targets, see
  https://chromium.googlesource.com/chromium/src/+/master/testing/libfuzzer/efficient_fuzzer.md#Code-Coverage

  * Sample workflow for running Blink web tests:

  python tools/code_coverage/coverage.py blink_tests \\
      -wt -b out/coverage -o out/report -f third_party/blink

  If you need to pass arguments to run_web_tests.py, use
    -wt='arguments to run_web_tests.py e.g. test directories'

  For more options, please refer to tools/code_coverage/coverage.py -h.

  For an overview of how code coverage works in Chromium, please refer to
  https://chromium.googlesource.com/chromium/src/+/master/docs/testing/code_coverage.md
"""

from __future__ import print_function

import sys

import argparse
import json
import logging
import multiprocessing
import os
import re
import shlex
import shutil
import subprocess
import urllib2

sys.path.append(
    os.path.join(
        os.path.dirname(__file__), os.path.pardir, os.path.pardir, 'tools',
        'clang', 'scripts'))
import update

sys.path.append(
    os.path.join(
        os.path.dirname(__file__), os.path.pardir, os.path.pardir,
        'third_party'))
from collections import defaultdict

import coverage_utils

# Absolute path to the code coverage tools binary. These paths can be
# overwritten by user specified coverage tool paths.
LLVM_BIN_DIR = os.path.join(update.LLVM_BUILD_DIR, 'bin')
LLVM_COV_PATH = os.path.join(LLVM_BIN_DIR, 'llvm-cov')
LLVM_PROFDATA_PATH = os.path.join(LLVM_BIN_DIR, 'llvm-profdata')

# Absolute path to the root of the checkout.
SRC_ROOT_PATH = None

# Build directory, the value is parsed from command line arguments.
BUILD_DIR = None

# Output directory for generated artifacts, the value is parsed from command
# line arguemnts.
OUTPUT_DIR = None

# Name of the file extension for profraw data files.
PROFRAW_FILE_EXTENSION = 'profraw'

# Name of the final profdata file, and this file needs to be passed to
# "llvm-cov" command in order to call "llvm-cov show" to inspect the
# line-by-line coverage of specific files.
PROFDATA_FILE_NAME = os.extsep.join(['coverage', 'profdata'])

# Name of the file with summary information generated by llvm-cov export.
SUMMARY_FILE_NAME = os.extsep.join(['summary', 'json'])

# Build arg required for generating code coverage data.
CLANG_COVERAGE_BUILD_ARG = 'use_clang_coverage'

LOGS_DIR_NAME = 'logs'

# Used to extract a mapping between directories and components.
COMPONENT_MAPPING_URL = (
    'https://storage.googleapis.com/chromium-owners/component_map.json')

# Caches the results returned by _GetBuildArgs, don't use this variable
# directly, call _GetBuildArgs instead.
_BUILD_ARGS = None

# Retry failed merges.
MERGE_RETRIES = 3

# Message to guide user to file a bug when everything else fails.
FILE_BUG_MESSAGE = (
    'If it persists, please file a bug with the command you used, git revision '
    'and args.gn config here: '
    'https://bugs.chromium.org/p/chromium/issues/entry?'
    'components=Infra%3ETest%3ECodeCoverage')

# String to replace with actual llvm profile path.
LLVM_PROFILE_FILE_PATH_SUBSTITUTION = '<llvm_profile_file_path>'


def _ConfigureLLVMCoverageTools(args):
  """Configures llvm coverage tools."""
  if args.coverage_tools_dir:
    llvm_bin_dir = coverage_utils.GetFullPath(args.coverage_tools_dir)
    global LLVM_COV_PATH
    global LLVM_PROFDATA_PATH
    LLVM_COV_PATH = os.path.join(llvm_bin_dir, 'llvm-cov')
    LLVM_PROFDATA_PATH = os.path.join(llvm_bin_dir, 'llvm-profdata')
  else:
    subprocess.check_call(
        ['tools/clang/scripts/update.py', '--package', 'coverage_tools'])

  if coverage_utils.GetHostPlatform() == 'win':
    LLVM_COV_PATH += '.exe'
    LLVM_PROFDATA_PATH += '.exe'

  coverage_tools_exist = (
      os.path.exists(LLVM_COV_PATH) and os.path.exists(LLVM_PROFDATA_PATH))
  assert coverage_tools_exist, ('Cannot find coverage tools, please make sure '
                                'both \'%s\' and \'%s\' exist.') % (
                                    LLVM_COV_PATH, LLVM_PROFDATA_PATH)


def _GetPathWithLLVMSymbolizerDir():
  """Add llvm-symbolizer directory to path for symbolized stacks."""
  path = os.getenv('PATH')
  dirs = path.split(os.pathsep)
  if LLVM_BIN_DIR in dirs:
    return path

  return path + os.pathsep + LLVM_BIN_DIR


def _GetTargetOS():
  """Returns the target os specified in args.gn file.

  Returns an empty string is target_os is not specified.
  """
  build_args = _GetBuildArgs()
  return build_args['target_os'] if 'target_os' in build_args else ''


def _IsIOS():
  """Returns true if the target_os specified in args.gn file is ios"""
  return _GetTargetOS() == 'ios'


def _GeneratePerFileLineByLineCoverageInFormat(binary_paths, profdata_file_path,
                                               filters, ignore_filename_regex,
                                               output_format):
  """Generates per file line-by-line coverage in html or text using
  'llvm-cov show'.

  For a file with absolute path /a/b/x.cc, a html/txt report is generated as:
  OUTPUT_DIR/coverage/a/b/x.cc.[html|txt]. For html format, an index html file
  is also generated as: OUTPUT_DIR/index.html.

  Args:
    binary_paths: A list of paths to the instrumented binaries.
    profdata_file_path: A path to the profdata file.
    filters: A list of directories and files to get coverage for.
    ignore_filename_regex: A regular expression for skipping source code files
                           with certain file paths.
    output_format: The output format of generated report files.
  """
  # llvm-cov show [options] -instr-profile PROFILE BIN [-object BIN,...]
  # [[-object BIN]] [SOURCES]
  # NOTE: For object files, the first one is specified as a positional argument,
  # and the rest are specified as keyword argument.
  logging.debug('Generating per file line by line coverage reports using '
                '"llvm-cov show" command.')

  subprocess_cmd = [
      LLVM_COV_PATH, 'show', '-format={}'.format(output_format),
      '-output-dir={}'.format(OUTPUT_DIR),
      '-instr-profile={}'.format(profdata_file_path), binary_paths[0]
  ]
  subprocess_cmd.extend(
      ['-object=' + binary_path for binary_path in binary_paths[1:]])
  _AddArchArgumentForIOSIfNeeded(subprocess_cmd, len(binary_paths))
  if coverage_utils.GetHostPlatform() in ['linux', 'mac']:
    subprocess_cmd.extend(['-Xdemangler', 'c++filt', '-Xdemangler', '-n'])
  subprocess_cmd.extend(filters)
  if ignore_filename_regex:
    subprocess_cmd.append('-ignore-filename-regex=%s' % ignore_filename_regex)

  subprocess.check_call(subprocess_cmd)

  logging.debug('Finished running "llvm-cov show" command.')


def _GetLogsDirectoryPath():
  """Path to the logs directory."""
  return os.path.join(
      coverage_utils.GetCoverageReportRootDirPath(OUTPUT_DIR), LOGS_DIR_NAME)


def _GetProfdataFilePath():
  """Path to the resulting .profdata file."""
  return os.path.join(
      coverage_utils.GetCoverageReportRootDirPath(OUTPUT_DIR),
      PROFDATA_FILE_NAME)


def _GetSummaryFilePath():
  """The JSON file that contains coverage summary written by llvm-cov export."""
  return os.path.join(
      coverage_utils.GetCoverageReportRootDirPath(OUTPUT_DIR),
      SUMMARY_FILE_NAME)


def _CreateCoverageProfileDataForTargets(targets, commands, jobs_count=None):
  """Builds and runs target to generate the coverage profile data.

  Args:
    targets: A list of targets to build with coverage instrumentation.
    commands: A list of commands used to run the targets.
    jobs_count: Number of jobs to run in parallel for building. If None, a
                default value is derived based on CPUs availability.

  Returns:
    A relative path to the generated profdata file.
  """
  _BuildTargets(targets, jobs_count)
  target_profdata_file_paths = _GetTargetProfDataPathsByExecutingCommands(
      targets, commands)
  coverage_profdata_file_path = (
      _CreateCoverageProfileDataFromTargetProfDataFiles(
          target_profdata_file_paths))

  for target_profdata_file_path in target_profdata_file_paths:
    os.remove(target_profdata_file_path)

  return coverage_profdata_file_path


def _BuildTargets(targets, jobs_count):
  """Builds target with Clang coverage instrumentation.

  This function requires current working directory to be the root of checkout.

  Args:
    targets: A list of targets to build with coverage instrumentation.
    jobs_count: Number of jobs to run in parallel for compilation. If None, a
                default value is derived based on CPUs availability.
  """
  logging.info('Building %s.', str(targets))
  autoninja = 'autoninja'
  if coverage_utils.GetHostPlatform() == 'win':
    autoninja += '.bat'

  subprocess_cmd = [autoninja, '-C', BUILD_DIR]
  if jobs_count is not None:
    subprocess_cmd.append('-j' + str(jobs_count))

  subprocess_cmd.extend(targets)
  subprocess.check_call(subprocess_cmd)
  logging.debug('Finished building %s.', str(targets))


def _GetTargetProfDataPathsByExecutingCommands(targets, commands):
  """Runs commands and returns the relative paths to the profraw data files.

  Args:
    targets: A list of targets built with coverage instrumentation.
    commands: A list of commands used to run the targets.

  Returns:
    A list of relative paths to the generated profraw data files.
  """
  logging.debug('Executing the test commands.')

  # Remove existing profraw data files.
  report_root_dir = coverage_utils.GetCoverageReportRootDirPath(OUTPUT_DIR)
  for file_or_dir in os.listdir(report_root_dir):
    if file_or_dir.endswith(PROFRAW_FILE_EXTENSION):
      os.remove(os.path.join(report_root_dir, file_or_dir))

  # Ensure that logs directory exists.
  if not os.path.exists(_GetLogsDirectoryPath()):
    os.makedirs(_GetLogsDirectoryPath())

  profdata_file_paths = []

  # Run all test targets to generate profraw data files.
  for target, command in zip(targets, commands):
    output_file_name = os.extsep.join([target + '_output', 'log'])
    output_file_path = os.path.join(_GetLogsDirectoryPath(), output_file_name)

    profdata_file_path = None
    for _ in xrange(MERGE_RETRIES):
      logging.info('Running command: "%s", the output is redirected to "%s".',
                   command, output_file_path)

      if _IsIOSCommand(command):
        # On iOS platform, due to lack of write permissions, profraw files are
        # generated outside of the OUTPUT_DIR, and the exact paths are contained
        # in the output of the command execution.
        output = _ExecuteIOSCommand(command, output_file_path)
      else:
        # On other platforms, profraw files are generated inside the OUTPUT_DIR.
        output = _ExecuteCommand(target, command, output_file_path)

      profraw_file_paths = []
      if _IsIOS():
        profraw_file_paths = [_GetProfrawDataFileByParsingOutput(output)]
      else:
        for file_or_dir in os.listdir(report_root_dir):
          if file_or_dir.endswith(PROFRAW_FILE_EXTENSION):
            profraw_file_paths.append(
                os.path.join(report_root_dir, file_or_dir))

      assert profraw_file_paths, (
          'Running target "%s" failed to generate any profraw data file, '
          'please make sure the binary exists, is properly instrumented and '
          'does not crash. %s' % (target, FILE_BUG_MESSAGE))

      assert isinstance(profraw_file_paths, list), (
          'Variable \'profraw_file_paths\' is expected to be of type \'list\', '
          'but it is a %s. %s' % (type(profraw_file_paths), FILE_BUG_MESSAGE))

      try:
        profdata_file_path = _CreateTargetProfDataFileFromProfRawFiles(
            target, profraw_file_paths)
        break
      except Exception:
        logging.info('Retrying...')
      finally:
        # Remove profraw files now so that they are not used in next iteration.
        for profraw_file_path in profraw_file_paths:
          os.remove(profraw_file_path)

    assert profdata_file_path, (
        'Failed to merge target "%s" profraw files after %d retries. %s' %
        (target, MERGE_RETRIES, FILE_BUG_MESSAGE))
    profdata_file_paths.append(profdata_file_path)

  logging.debug('Finished executing the test commands.')

  return profdata_file_paths


def _GetEnvironmentVars(profraw_file_path):
  """Return environment vars for subprocess, given a profraw file path."""
  env = os.environ.copy()
  env.update({
      'LLVM_PROFILE_FILE': profraw_file_path,
      'PATH': _GetPathWithLLVMSymbolizerDir()
  })
  return env


def _ExecuteCommand(target, command, output_file_path):
  """Runs a single command and generates a profraw data file."""
  # Per Clang "Source-based Code Coverage" doc:
  #
  # "%p" expands out to the process ID. It's not used by this scripts due to:
  # 1) If a target program spawns too many processess, it may exhaust all disk
  #    space available. For example, unit_tests writes thousands of .profraw
  #    files each of size 1GB+.
  # 2) If a target binary uses shared libraries, coverage profile data for them
  #    will be missing, resulting in incomplete coverage reports.
  #
  # "%Nm" expands out to the instrumented binary's signature. When this pattern
  # is specified, the runtime creates a pool of N raw profiles which are used
  # for on-line profile merging. The runtime takes care of selecting a raw
  # profile from the pool, locking it, and updating it before the program exits.
  # N must be between 1 and 9. The merge pool specifier can only occur once per
  # filename pattern.
  #
  # "%1m" is used when tests run in single process, such as fuzz targets.
  #
  # For other cases, "%4m" is chosen as it creates some level of parallelism,
  # but it's not too big to consume too much computing resource or disk space.
  profile_pattern_string = '%1m' if _IsFuzzerTarget(target) else '%4m'
  expected_profraw_file_name = os.extsep.join(
      [target, profile_pattern_string, PROFRAW_FILE_EXTENSION])
  expected_profraw_file_path = os.path.join(
      coverage_utils.GetCoverageReportRootDirPath(OUTPUT_DIR),
      expected_profraw_file_name)
  command = command.replace(LLVM_PROFILE_FILE_PATH_SUBSTITUTION,
                            expected_profraw_file_path)

  try:
    # Some fuzz targets or tests may write into stderr, redirect it as well.
    with open(output_file_path, 'wb') as output_file_handle:
      subprocess.check_call(
          shlex.split(command),
          stdout=output_file_handle,
          stderr=subprocess.STDOUT,
          env=_GetEnvironmentVars(expected_profraw_file_path))
  except subprocess.CalledProcessError as e:
    logging.warning('Command: "%s" exited with non-zero return code.', command)

  return open(output_file_path, 'rb').read()


def _IsFuzzerTarget(target):
  """Returns true if the target is a fuzzer target."""
  build_args = _GetBuildArgs()
  use_libfuzzer = ('use_libfuzzer' in build_args and
                   build_args['use_libfuzzer'] == 'true')
  return use_libfuzzer and target.endswith('_fuzzer')


def _ExecuteIOSCommand(command, output_file_path):
  """Runs a single iOS command and generates a profraw data file.

  iOS application doesn't have write access to folders outside of the app, so
  it's impossible to instruct the app to flush the profraw data file to the
  desired location. The profraw data file will be generated somewhere within the
  application's Documents folder, and the full path can be obtained by parsing
  the output.
  """
  assert _IsIOSCommand(command)

  # After running tests, iossim generates a profraw data file, it won't be
  # needed anyway, so dump it into the OUTPUT_DIR to avoid polluting the
  # checkout.
  iossim_profraw_file_path = os.path.join(
      OUTPUT_DIR, os.extsep.join(['iossim', PROFRAW_FILE_EXTENSION]))
  command = command.replace(LLVM_PROFILE_FILE_PATH_SUBSTITUTION,
                            iossim_profraw_file_path)

  try:
    with open(output_file_path, 'wb') as output_file_handle:
      subprocess.check_call(
          shlex.split(command),
          stdout=output_file_handle,
          stderr=subprocess.STDOUT,
          env=_GetEnvironmentVars(iossim_profraw_file_path))
  except subprocess.CalledProcessError as e:
    # iossim emits non-zero return code even if tests run successfully, so
    # ignore the return code.
    pass

  return open(output_file_path, 'rb').read()


def _GetProfrawDataFileByParsingOutput(output):
  """Returns the path to the profraw data file obtained by parsing the output.

  The output of running the test target has no format, but it is guaranteed to
  have a single line containing the path to the generated profraw data file.
  NOTE: This should only be called when target os is iOS.
  """
  assert _IsIOS()

  output_by_lines = ''.join(output).splitlines()
  profraw_file_pattern = re.compile('.*Coverage data at (.*coverage\.profraw).')

  for line in output_by_lines:
    result = profraw_file_pattern.match(line)
    if result:
      return result.group(1)

  assert False, ('No profraw data file was generated, did you call '
                 'coverage_util::ConfigureCoverageReportPath() in test setup? '
                 'Please refer to base/test/test_support_ios.mm for example.')


def _CreateCoverageProfileDataFromTargetProfDataFiles(profdata_file_paths):
  """Returns a relative path to coverage profdata file by merging target
  profdata files.

  Args:
    profdata_file_paths: A list of relative paths to the profdata data files
                         that are to be merged.

  Returns:
    A relative path to the merged coverage profdata file.

  Raises:
    CalledProcessError: An error occurred merging profdata files.
  """
  logging.info('Creating the coverage profile data file.')
  logging.debug('Merging target profraw files to create target profdata file.')
  profdata_file_path = _GetProfdataFilePath()
  try:
    subprocess_cmd = [
        LLVM_PROFDATA_PATH, 'merge', '-o', profdata_file_path, '-sparse=true'
    ]
    subprocess_cmd.extend(profdata_file_paths)

    output = subprocess.check_output(subprocess_cmd)
    logging.debug('Merge output: %s', output)
  except subprocess.CalledProcessError as error:
    logging.error(
        'Failed to merge target profdata files to create coverage profdata. %s',
        FILE_BUG_MESSAGE)
    raise error

  logging.debug('Finished merging target profdata files.')
  logging.info('Code coverage profile data is created as: "%s".',
               profdata_file_path)
  return profdata_file_path


def _CreateTargetProfDataFileFromProfRawFiles(target, profraw_file_paths):
  """Returns a relative path to target profdata file by merging target
  profraw files.

  Args:
    profraw_file_paths: A list of relative paths to the profdata data files
                         that are to be merged.

  Returns:
    A relative path to the merged coverage profdata file.

  Raises:
    CalledProcessError: An error occurred merging profdata files.
  """
  logging.info('Creating target profile data file.')
  logging.debug('Merging target profraw files to create target profdata file.')
  profdata_file_path = os.path.join(OUTPUT_DIR, '%s.profdata' % target)

  try:
    subprocess_cmd = [
        LLVM_PROFDATA_PATH, 'merge', '-o', profdata_file_path, '-sparse=true'
    ]
    subprocess_cmd.extend(profraw_file_paths)

    output = subprocess.check_output(subprocess_cmd)
    logging.debug('Merge output: %s', output)
  except subprocess.CalledProcessError as error:
    logging.error(
        'Failed to merge target profraw files to create target profdata.')
    raise error

  logging.debug('Finished merging target profraw files.')
  logging.info('Target "%s" profile data is created as: "%s".', target,
               profdata_file_path)
  return profdata_file_path


def _GeneratePerFileCoverageSummary(binary_paths, profdata_file_path, filters,
                                    ignore_filename_regex):
  """Generates per file coverage summary using "llvm-cov export" command."""
  # llvm-cov export [options] -instr-profile PROFILE BIN [-object BIN,...]
  # [[-object BIN]] [SOURCES].
  # NOTE: For object files, the first one is specified as a positional argument,
  # and the rest are specified as keyword argument.
  logging.debug('Generating per-file code coverage summary using "llvm-cov '
                'export -summary-only" command.')
  subprocess_cmd = [
      LLVM_COV_PATH, 'export', '-summary-only',
      '-instr-profile=' + profdata_file_path, binary_paths[0]
  ]
  subprocess_cmd.extend(
      ['-object=' + binary_path for binary_path in binary_paths[1:]])
  _AddArchArgumentForIOSIfNeeded(subprocess_cmd, len(binary_paths))
  subprocess_cmd.extend(filters)
  if ignore_filename_regex:
    subprocess_cmd.append('-ignore-filename-regex=%s' % ignore_filename_regex)

  export_output = subprocess.check_output(subprocess_cmd)

  # Write output on the disk to be used by code coverage bot.
  with open(_GetSummaryFilePath(), 'w') as f:
    f.write(export_output)

  return export_output


def _AddArchArgumentForIOSIfNeeded(cmd_list, num_archs):
  """Appends -arch arguments to the command list if it's ios platform.

  iOS binaries are universal binaries, and require specifying the architecture
  to use, and one architecture needs to be specified for each binary.
  """
  if _IsIOS():
    cmd_list.extend(['-arch=x86_64'] * num_archs)


def _GetBinaryPath(command):
  """Returns a relative path to the binary to be run by the command.

  Currently, following types of commands are supported (e.g. url_unittests):
  1. Run test binary direcly: "out/coverage/url_unittests <arguments>"
  2. Use xvfb.
    2.1. "python testing/xvfb.py out/coverage/url_unittests <arguments>"
    2.2. "testing/xvfb.py out/coverage/url_unittests <arguments>"
  3. Use iossim to run tests on iOS platform, please refer to testing/iossim.mm
    for its usage.
    3.1. "out/Coverage-iphonesimulator/iossim
          <iossim_arguments> -c <app_arguments>
          out/Coverage-iphonesimulator/url_unittests.app"

  Args:
    command: A command used to run a target.

  Returns:
    A relative path to the binary.
  """
  xvfb_script_name = os.extsep.join(['xvfb', 'py'])

  command_parts = shlex.split(command)
  if os.path.basename(command_parts[0]) == 'python':
    assert os.path.basename(command_parts[1]) == xvfb_script_name, (
        'This tool doesn\'t understand the command: "%s".' % command)
    return command_parts[2]

  if os.path.basename(command_parts[0]) == xvfb_script_name:
    return command_parts[1]

  if _IsIOSCommand(command):
    # For a given application bundle, the binary resides in the bundle and has
    # the same name with the application without the .app extension.
    app_path = command_parts[1].rstrip(os.path.sep)
    app_name = os.path.splitext(os.path.basename(app_path))[0]
    return os.path.join(app_path, app_name)

  return command_parts[0]


def _IsIOSCommand(command):
  """Returns true if command is used to run tests on iOS platform."""
  return os.path.basename(shlex.split(command)[0]) == 'iossim'


def _VerifyTargetExecutablesAreInBuildDirectory(commands):
  """Verifies that the target executables specified in the commands are inside
  the given build directory."""
  for command in commands:
    binary_path = _GetBinaryPath(command)
    binary_absolute_path = coverage_utils.GetFullPath(binary_path)
    assert binary_absolute_path.startswith(BUILD_DIR + os.sep), (
        'Target executable "%s" in command: "%s" is outside of '
        'the given build directory: "%s".' % (binary_path, command, BUILD_DIR))


def _ValidateBuildingWithClangCoverage():
  """Asserts that targets are built with Clang coverage enabled."""
  build_args = _GetBuildArgs()

  if (CLANG_COVERAGE_BUILD_ARG not in build_args or
      build_args[CLANG_COVERAGE_BUILD_ARG] != 'true'):
    assert False, ('\'{} = true\' is required in args.gn.'
                  ).format(CLANG_COVERAGE_BUILD_ARG)


def _ValidateCurrentPlatformIsSupported():
  """Asserts that this script suports running on the current platform"""
  target_os = _GetTargetOS()
  if target_os:
    current_platform = target_os
  else:
    current_platform = coverage_utils.GetHostPlatform()

  assert current_platform in [
      'linux', 'mac', 'chromeos', 'ios', 'win'
  ], ('Coverage is only supported on linux, mac, chromeos, ios and win.')


def _GetBuildArgs():
  """Parses args.gn file and returns results as a dictionary.

  Returns:
    A dictionary representing the build args.
  """
  global _BUILD_ARGS
  if _BUILD_ARGS is not None:
    return _BUILD_ARGS

  _BUILD_ARGS = {}
  build_args_path = os.path.join(BUILD_DIR, 'args.gn')
  assert os.path.exists(build_args_path), ('"%s" is not a build directory, '
                                           'missing args.gn file.' % BUILD_DIR)
  with open(build_args_path) as build_args_file:
    build_args_lines = build_args_file.readlines()

  for build_arg_line in build_args_lines:
    build_arg_without_comments = build_arg_line.split('#')[0]
    key_value_pair = build_arg_without_comments.split('=')
    if len(key_value_pair) != 2:
      continue

    key = key_value_pair[0].strip()

    # Values are wrapped within a pair of double-quotes, so remove the leading
    # and trailing double-quotes.
    value = key_value_pair[1].strip().strip('"')
    _BUILD_ARGS[key] = value

  return _BUILD_ARGS


def _VerifyPathsAndReturnAbsolutes(paths):
  """Verifies that the paths specified in |paths| exist and returns absolute
  versions.

  Args:
    paths: A list of files or directories.
  """
  absolute_paths = []
  for path in paths:
    absolute_path = os.path.join(SRC_ROOT_PATH, path)
    assert os.path.exists(absolute_path), ('Path: "%s" doesn\'t exist.' % path)

    absolute_paths.append(absolute_path)

  return absolute_paths


def _GetBinaryPathsFromTargets(targets, build_dir):
  """Return binary paths from target names."""
  # FIXME: Derive output binary from target build definitions rather than
  # assuming that it is always the same name.
  binary_paths = []
  for target in targets:
    binary_path = os.path.join(build_dir, target)
    if coverage_utils.GetHostPlatform() == 'win':
      binary_path += '.exe'

    if os.path.exists(binary_path):
      binary_paths.append(binary_path)
    else:
      logging.warning(
          'Target binary "%s" not found in build directory, skipping.',
          os.path.basename(binary_path))

  return binary_paths


def _GetCommandForWebTests(arguments):
  """Return command to run for blink web tests."""
  command_list = [
      'python', 'testing/xvfb.py', 'python',
      'third_party/blink/tools/run_web_tests.py',
      '--additional-driver-flag=--no-sandbox',
      '--additional-env-var=LLVM_PROFILE_FILE=%s' %
      LLVM_PROFILE_FILE_PATH_SUBSTITUTION,
      '--child-processes=%d' % max(1, int(multiprocessing.cpu_count() / 2)),
      '--disable-breakpad', '--no-show-results', '--skip-failing-tests',
      '--target=%s' % os.path.basename(BUILD_DIR), '--time-out-ms=30000'
  ]
  if arguments.strip():
    command_list.append(arguments)
  return ' '.join(command_list)


def _GetBinaryPathForWebTests():
  """Return binary path used to run blink web tests."""
  host_platform = coverage_utils.GetHostPlatform()
  if host_platform == 'win':
    return os.path.join(BUILD_DIR, 'content_shell.exe')
  elif host_platform == 'linux':
    return os.path.join(BUILD_DIR, 'content_shell')
  elif host_platform == 'mac':
    return os.path.join(BUILD_DIR, 'Content Shell.app', 'Contents', 'MacOS',
                        'Content Shell')
  else:
    assert False, 'This platform is not supported for web tests.'


def _SetupOutputDir():
  """Setup output directory."""
  if os.path.exists(OUTPUT_DIR):
    shutil.rmtree(OUTPUT_DIR)

  # Creates |OUTPUT_DIR| and its platform sub-directory.
  os.makedirs(coverage_utils.GetCoverageReportRootDirPath(OUTPUT_DIR))


def _SetMacXcodePath():
  """Set DEVELOPER_DIR to the path to hermetic Xcode.app on Mac OS X."""
  if sys.platform != 'darwin':
    return

  xcode_path = os.path.join(SRC_ROOT_PATH, 'build', 'mac_files', 'Xcode.app')
  if os.path.exists(xcode_path):
    os.environ['DEVELOPER_DIR'] = xcode_path


def _ParseCommandArguments():
  """Adds and parses relevant arguments for tool comands.

  Returns:
    A dictionary representing the arguments.
  """
  arg_parser = argparse.ArgumentParser()
  arg_parser.usage = __doc__

  arg_parser.add_argument(
      '-b',
      '--build-dir',
      type=str,
      required=True,
      help='The build directory, the path needs to be relative to the root of '
      'the checkout.')

  arg_parser.add_argument(
      '-o',
      '--output-dir',
      type=str,
      required=True,
      help='Output directory for generated artifacts.')

  arg_parser.add_argument(
      '-c',
      '--command',
      action='append',
      required=False,
      help='Commands used to run test targets, one test target needs one and '
      'only one command, when specifying commands, one should assume the '
      'current working directory is the root of the checkout. This option is '
      'incompatible with -p/--profdata-file option.')

  arg_parser.add_argument(
      '-wt',
      '--web-tests',
      nargs='?',
      type=str,
      const=' ',
      required=False,
      help='Run blink web tests. Support passing arguments to run_web_tests.py')

  arg_parser.add_argument(
      '-p',
      '--profdata-file',
      type=str,
      required=False,
      help='Path to profdata file to use for generating code coverage reports. '
      'This can be useful if you generated the profdata file seperately in '
      'your own test harness. This option is ignored if run command(s) are '
      'already provided above using -c/--command option.')

  arg_parser.add_argument(
      '-f',
      '--filters',
      action='append',
      required=False,
      help='Directories or files to get code coverage for, and all files under '
      'the directories are included recursively.')

  arg_parser.add_argument(
      '-i',
      '--ignore-filename-regex',
      type=str,
      help='Skip source code files with file paths that match the given '
      'regular expression. For example, use -i=\'.*/out/.*|.*/third_party/.*\' '
      'to exclude files in third_party/ and out/ folders from the report.')

  arg_parser.add_argument(
      '--no-file-view',
      action='store_true',
      help='Don\'t generate the file view in the coverage report. When there '
      'are large number of html files, the file view becomes heavy and may '
      'cause the browser to freeze, and this argument comes handy.')

  arg_parser.add_argument(
      '--no-component-view',
      action='store_true',
      help='Don\'t generate the component view in the coverage report.')

  arg_parser.add_argument(
      '--coverage-tools-dir',
      type=str,
      help='Path of the directory where LLVM coverage tools (llvm-cov, '
      'llvm-profdata) exist. This should be only needed if you are testing '
      'against a custom built clang revision. Otherwise, we pick coverage '
      'tools automatically from your current source checkout.')

  arg_parser.add_argument(
      '-j',
      '--jobs',
      type=int,
      default=None,
      help='Run N jobs to build in parallel. If not specified, a default value '
      'will be derived based on CPUs and goma availability. Please refer to '
      '\'autoninja -h\' for more details.')

  arg_parser.add_argument(
      '--format',
      type=str,
      default='html',
      help='Output format of the "llvm-cov show" command. The supported '
      'formats are "text" and "html".')

  arg_parser.add_argument(
      '-v',
      '--verbose',
      action='store_true',
      help='Prints additional output for diagnostics.')

  arg_parser.add_argument(
      '-l', '--log_file', type=str, help='Redirects logs to a file.')

  arg_parser.add_argument(
      'targets',
      nargs='+',
      help='The names of the test targets to run. If multiple run commands are '
      'specified using the -c/--command option, then the order of targets and '
      'commands must match, otherwise coverage generation will fail.')

  args = arg_parser.parse_args()
  return args


def Main():
  """Execute tool commands."""

  # Change directory to source root to aid in relative paths calculations.
  global SRC_ROOT_PATH
  SRC_ROOT_PATH = coverage_utils.GetFullPath(
      os.path.join(os.path.dirname(__file__), os.path.pardir, os.path.pardir))
  os.chdir(SRC_ROOT_PATH)

  # Setup coverage binaries even when script is called with empty params. This
  # is used by coverage bot for initial setup.
  if len(sys.argv) == 1:
    subprocess.check_call(
        ['tools/clang/scripts/update.py', '--package', 'coverage_tools'])
    print(__doc__)
    return

  args = _ParseCommandArguments()
  coverage_utils.ConfigureLogging(verbose=args.verbose, log_file=args.log_file)
  _ConfigureLLVMCoverageTools(args)

  global BUILD_DIR
  BUILD_DIR = coverage_utils.GetFullPath(args.build_dir)

  global OUTPUT_DIR
  OUTPUT_DIR = coverage_utils.GetFullPath(args.output_dir)

  assert args.web_tests or args.command or args.profdata_file, (
      'Need to either provide commands to run using -c/--command option OR '
      'provide prof-data file as input using -p/--profdata-file option OR '
      'run web tests using -wt/--run-web-tests.')

  assert not args.command or (len(args.targets) == len(args.command)), (
      'Number of targets must be equal to the number of test commands.')

  assert os.path.exists(BUILD_DIR), (
      'Build directory: "%s" doesn\'t exist. '
      'Please run "gn gen" to generate.' % BUILD_DIR)

  _ValidateCurrentPlatformIsSupported()
  _ValidateBuildingWithClangCoverage()

  absolute_filter_paths = []
  if args.filters:
    absolute_filter_paths = _VerifyPathsAndReturnAbsolutes(args.filters)

  _SetupOutputDir()

  # Get .profdata file and list of binary paths.
  if args.web_tests:
    commands = [_GetCommandForWebTests(args.web_tests)]
    profdata_file_path = _CreateCoverageProfileDataForTargets(
        args.targets, commands, args.jobs)
    binary_paths = [_GetBinaryPathForWebTests()]
  elif args.command:
    for i in range(len(args.command)):
      assert not 'run_web_tests.py' in args.command[i], (
          'run_web_tests.py is not supported via --command argument. '
          'Please use --run-web-tests argument instead.')

    # A list of commands are provided. Run them to generate profdata file, and
    # create a list of binary paths from parsing commands.
    _VerifyTargetExecutablesAreInBuildDirectory(args.command)
    profdata_file_path = _CreateCoverageProfileDataForTargets(
        args.targets, args.command, args.jobs)
    binary_paths = [_GetBinaryPath(command) for command in args.command]
  else:
    # An input prof-data file is already provided. Just calculate binary paths.
    profdata_file_path = args.profdata_file
    binary_paths = _GetBinaryPathsFromTargets(args.targets, args.build_dir)

  # If the checkout uses the hermetic xcode binaries, then otool must be
  # directly invoked. The indirection via /usr/bin/otool won't work unless
  # there's an actual system install of Xcode.
  otool_path = None
  if sys.platform == 'darwin':
    hermetic_otool_path = os.path.join(
        SRC_ROOT_PATH, 'build', 'mac_files', 'xcode_binaries', 'Contents',
        'Developer', 'Toolchains', 'XcodeDefault.xctoolchain', 'usr', 'bin',
        'otool')
    if os.path.exists(hermetic_otool_path):
      otool_path = hermetic_otool_path
  if sys.platform.startswith('linux') or sys.platform.startswith('darwin'):
    binary_paths.extend(
        coverage_utils.GetSharedLibraries(binary_paths, BUILD_DIR, otool_path))

  assert args.format == 'html' or args.format == 'text', (
      '%s is not a valid output format for "llvm-cov show". Only "text" and '
      '"html" formats are supported.' % (args.format))
  logging.info('Generating code coverage report in %s (this can take a while '
               'depending on size of target!).' % (args.format))
  per_file_summary_data = _GeneratePerFileCoverageSummary(
      binary_paths, profdata_file_path, absolute_filter_paths,
      args.ignore_filename_regex)
  _GeneratePerFileLineByLineCoverageInFormat(
      binary_paths, profdata_file_path, absolute_filter_paths,
      args.ignore_filename_regex, args.format)
  component_mappings = None
  if not args.no_component_view:
    component_mappings = json.load(urllib2.urlopen(COMPONENT_MAPPING_URL))

  # Call prepare here.
  processor = coverage_utils.CoverageReportPostProcessor(
      OUTPUT_DIR,
      SRC_ROOT_PATH,
      per_file_summary_data,
      no_component_view=args.no_component_view,
      no_file_view=args.no_file_view,
      component_mappings=component_mappings)

  if args.format == 'html':
    processor.PrepareHtmlReport()


if __name__ == '__main__':
  sys.exit(Main())
