#!/usr/bin/python

# Copyright (c) 2011 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unit tests for gmerge."""

import os
import unittest

import gmerge


class Flags(object):
  def __init__(self, dictionary):
    self.__dict__.update(dictionary)


class GMergeTest(unittest.TestCase):
  """Test for gmerge."""

  def setUp(self):
    self.lsb_release_lines = [
        'COREOS_RELEASE_BOARD=x86-mario\r\n',
        'COREOS_DEVSERVER=http://localhost:8080/\n']

  def testLsbRelease(self):
    merger = gmerge.GMerger(self.lsb_release_lines)
    self.assertEqual({'COREOS_RELEASE_BOARD': 'x86-mario',
                      'COREOS_DEVSERVER': 'http://localhost:8080/'},
                     merger.lsb_release)

  def testPostData(self):
    old_env = os.environ
    os.environ = {}
    os.environ['USE'] = 'a b c d +e'
    gmerge.FLAGS = Flags({'accept_stable': 'blah',
                          'deep': False,
                          'usepkg': False})

    merger = gmerge.GMerger(self.lsb_release_lines)
    self.assertEqual(
        'use=a+b+c+d+%2Be&board=x86-mario&deep=&pkg=package_name&usepkg=&'
        'accept_stable=blah',
        merger.GeneratePackageRequest('package_name'))
    os.environ = old_env


if __name__ == '__main__':
  unittest.main()
