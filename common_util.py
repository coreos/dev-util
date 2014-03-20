# Copyright (c) 2012 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Helper class for interacting with the Dev Server."""

import base64
import binascii
import distutils.version
import errno
import hashlib
import os
import random
import re
import shutil
import time

import lockfile

import gsutil_util
import log_util


# Module-local log function.
def _Log(message, *args):
  return log_util.LogWithTag('UTIL', message, *args)


AU_BASE = 'au'
NTON_DIR_SUFFIX = '_nton'
MTON_DIR_SUFFIX = '_mton'
UPLOADED_LIST = 'UPLOADED'
DEVSERVER_LOCK_FILE = 'devserver'

_HASH_BLOCK_SIZE = 8192


def CommaSeparatedList(value_list, is_quoted=False):
  """Concatenates a list of strings.

  This turns ['a', 'b', 'c'] into a single string 'a, b and c'. It optionally
  adds quotes (`a') around each element. Used for logging.

  """
  if is_quoted:
    value_list = ["`" + value + "'" for value in value_list]

  if len(value_list) > 1:
    return (', '.join(value_list[:-1]) + ' and ' + value_list[-1])
  elif value_list:
    return value_list[0]
  else:
    return ''

class CommonUtilError(Exception):
  """Exception classes used by this module."""
  pass



def SafeSandboxAccess(static_dir, path):
  """Verify that the path is in static_dir.

  Args:
    static_dir: Directory where builds are served from.
    path: Path to verify.

  Returns:
    True if path is in static_dir, False otherwise
  """
  static_dir = os.path.realpath(static_dir)
  path = os.path.realpath(path)
  return (path.startswith(static_dir) and path != static_dir)


def AcquireLock(static_dir, tag, create_once=True):
  """Acquires a lock for a given tag.

  Creates a directory for the specified tag, and atomically creates a lock file
  in it. This tells other components the resource/task represented by the tag
  is unavailable.

  Args:
    static_dir:  Directory where builds are served from.
    tag:         Unique resource/task identifier. Use '/' for nested tags.
    create_once: Determines whether the directory must be freshly created; this
                 preserves previous semantics of the lock acquisition.

  Returns:
    Path to the created directory or None if creation failed.

  Raises:
    CommonUtilError: If lock can't be acquired.
  """
  build_dir = os.path.join(static_dir, tag)
  if not SafeSandboxAccess(static_dir, build_dir):
    raise CommonUtilError('Invalid tag "%s".' % tag)

  # Create the directory.
  is_created = False
  try:
    os.makedirs(build_dir)
    is_created = True
  except OSError, e:
    if e.errno == errno.EEXIST:
      if create_once:
        raise CommonUtilError(str(e))
    else:
      raise

  # Lock the directory.
  try:
    lock = lockfile.FileLock(os.path.join(build_dir, DEVSERVER_LOCK_FILE))
    lock.acquire(timeout=0)
  except lockfile.AlreadyLocked, e:
    raise CommonUtilError(str(e))
  except:
    # In any other case, remove the directory if we actually created it, so
    # that subsequent attempts won't fail to re-create it.
    if is_created:
      shutil.rmtree(build_dir)
    raise

  return build_dir


def ReleaseLock(static_dir, tag, destroy=False):
  """Releases the lock for a given tag.

  Optionally, removes the locked directory entirely.

  Args:
    static_dir: Directory where builds are served from.
    tag:        Unique resource/task identifier. Use '/' for nested tags.
    destroy:    Determines whether the locked directory should be removed
                entirely.

  Raises:
    CommonUtilError: If lock can't be released.
  """
  build_dir = os.path.join(static_dir, tag)
  if not SafeSandboxAccess(static_dir, build_dir):
    raise CommonUtilError('Invalid tag "%s".' % tag)

  lock = lockfile.FileLock(os.path.join(build_dir, DEVSERVER_LOCK_FILE))
  try:
    lock.break_lock()
    if destroy:
      shutil.rmtree(build_dir)
  except Exception, e:
    raise CommonUtilError(str(e))


def GetLatestBuildVersion(static_dir, target, milestone=None):
  """Retrieves the latest build version for a given board.

  Args:
    static_dir: Directory where builds are served from.
    target: The build target, typically a combination of the board and the
        type of build e.g. x86-mario-release.
    milestone: For latest build set to None, for builds only in a specific
        milestone set to a str of format Rxx (e.g. R16). Default: None.

  Returns:
    If latest found, a full build string is returned e.g. R17-1234.0.0-a1-b983.
    If no latest is found for some reason or another a '' string is returned.

  Raises:
    CommonUtilError: If for some reason the latest build cannot be
        deteremined, this could be due to the dir not existing or no builds
        being present after filtering on milestone.
  """
  target_path = os.path.join(static_dir, target)
  if not os.path.isdir(target_path):
    raise CommonUtilError('Cannot find path %s' % target_path)

  builds = [distutils.version.LooseVersion(build) for build in
            os.listdir(target_path)]

  if milestone and builds:
    # Check if milestone Rxx is in the string representation of the build.
    builds = filter(lambda x: milestone.upper() in str(x), builds)

  if not builds:
    raise CommonUtilError('Could not determine build for %s' % target)

  return str(max(builds))


def GetControlFile(static_dir, build, control_path):
  """Attempts to pull the requested control file from the Dev Server.

  Args:
    static_dir: Directory where builds are served from.
    build: Fully qualified build string; e.g. R17-1234.0.0-a1-b983.
    control_path: Path to control file on Dev Server relative to Autotest root.

  Raises:
    CommonUtilError: If lock can't be acquired.

  Returns:
    Content of the requested control file.
  """
  # Be forgiving if the user passes in the control_path with a leading /
  control_path = control_path.lstrip('/')
  control_path = os.path.join(static_dir, build, 'autotest',
                              control_path)
  if not SafeSandboxAccess(static_dir, control_path):
    raise CommonUtilError('Invalid control file "%s".' % control_path)

  if not os.path.exists(control_path):
    # TODO(scottz): Come up with some sort of error mechanism.
    # crosbug.com/25040
    return 'Unknown control path %s' % control_path

  with open(control_path, 'r') as control_file:
    return control_file.read()


def GetControlFileList(static_dir, build):
  """List all control|control. files in the specified board/build path.

  Args:
    static_dir: Directory where builds are served from.
    build: Fully qualified build string; e.g. R17-1234.0.0-a1-b983.

  Raises:
    CommonUtilError: If path is outside of sandbox.

  Returns:
    String of each file separated by a newline.
  """
  autotest_dir = os.path.join(static_dir, build, 'autotest/')
  if not SafeSandboxAccess(static_dir, autotest_dir):
    raise CommonUtilError('Autotest dir not in sandbox "%s".' % autotest_dir)

  control_files = set()
  if not os.path.exists(autotest_dir):
    # TODO(scottz): Come up with some sort of error mechanism.
    # crosbug.com/25040
    return 'Unknown build path %s' % autotest_dir

  for entry in os.walk(autotest_dir):
    dir_path, _, files = entry
    for file_entry in files:
      if file_entry.startswith('control.') or file_entry == 'control':
        control_files.add(os.path.join(dir_path,
                                       file_entry).replace(autotest_dir, ''))

  return '\n'.join(control_files)


def GetFileSize(file_path):
  """Returns the size in bytes of the file given."""
  return os.path.getsize(file_path)


# Hashlib is strange and doesn't actually define these in a sane way that
# pylint can find them. Disable checks for them.
# pylint: disable=E1101,W0106
def GetFileHashes(file_path, do_sha1=False, do_sha256=False, do_md5=False):
  """Computes and returns a list of requested hashes.

  Args:
    file_path: path to file to be hashed
    do_sha1:   whether or not to compute a SHA1 hash
    do_sha256: whether or not to compute a SHA256 hash
    do_md5:    whether or not to compute a MD5 hash
  Returns:
    A dictionary containing binary hash values, keyed by 'sha1', 'sha256' and
    'md5', respectively.
  """
  hashes = {}
  if (do_sha1 or do_sha256 or do_md5):
    # Initialize hashers.
    hasher_sha1 = hashlib.sha1() if do_sha1 else None
    hasher_sha256 = hashlib.sha256() if do_sha256 else None
    hasher_md5 = hashlib.md5() if do_md5 else None

    # Read blocks from file, update hashes.
    with open(file_path, 'rb') as fd:
      while True:
        block = fd.read(_HASH_BLOCK_SIZE)
        if not block:
          break
        hasher_sha1 and hasher_sha1.update(block)
        hasher_sha256 and hasher_sha256.update(block)
        hasher_md5 and hasher_md5.update(block)

    # Update return values.
    if hasher_sha1:
      hashes['sha1'] = hasher_sha1.digest()
    if hasher_sha256:
      hashes['sha256'] = hasher_sha256.digest()
    if hasher_md5:
      hashes['md5'] = hasher_md5.digest()

  return hashes


def GetFileSha1(file_path):
  """Returns the SHA1 checksum of the file given (base64 encoded)."""
  return base64.b64encode(GetFileHashes(file_path, do_sha1=True)['sha1'])


def GetFileSha256(file_path):
  """Returns the SHA256 checksum of the file given (base64 encoded)."""
  return base64.b64encode(GetFileHashes(file_path, do_sha256=True)['sha256'])


def GetFileMd5(file_path):
  """Returns the MD5 checksum of the file given (hex encoded)."""
  return binascii.hexlify(GetFileHashes(file_path, do_md5=True)['md5'])


def CopyFile(source, dest):
  """Copies a file from |source| to |dest|."""
  _Log('Copy File %s -> %s' % (source, dest))
  shutil.copy(source, dest)
