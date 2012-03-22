# Copyright (c) 2011 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import binascii
import glob
import os
import re
import time
import tools
from tools import CmdError

def RoundUp(value, boundary):
  """Align a value to the next power of 2 boundary.

  Args:
    value: The value to align.
    boundary: The boundary value, e.g. 4096. Must be a power of 2.

  Returns:
    The rounded-up value.
  """
  return (value + boundary - 1) & ~(boundary - 1)


class WriteFirmware:
  """Write firmware to a Tegra 2 board using USB A-A cable.

  This class handles re-reflashing a board with new firmware using the Tegra's
  built-in boot ROM feature. This works by putting the chip into a special mode
  where it ignores any available firmware and instead reads it from a connected
  host machine over USB.

  In our case we use that feature to send U-Boot along with a suitable payload
  and instructions to flash it to SPI flash. The payload is itself normally a
  full Chrome OS image consisting of U-Boot, some keys and verification
  information, images and a map of the flash memory.
  """
  def __init__(self, tools, fdt, output):
    """Set up a new WriteFirmware object.

    Args:
      tools: A tools library for us to use.
      fdt: An fdt which gives us some info that we need.
      output: An output object to use for printing progress and messages.
    """
    self._tools = tools
    self._fdt = fdt
    self._out = output
    self.text_base = self._fdt.GetInt('/chromeos-config', 'textbase');

    # For speed, use the 'update' algorithm and don't verify
    self.update = True
    self.verify = False

  def _GetFlashScript(self, payload_size, update, verify, boot_type, checksum,
                      bus='0'):
    """Get the U-Boot boot command needed to flash U-Boot.

    We leave a marker in the string for the load address of the image,
    since this depends on the size of this script. This can be replaced by
    the caller provided that the marker length is unchanged.

    Args:
      payload_size: Size of payload in bytes.
      update: Use faster update algorithm rather then full device erase
      verify: Verify the write by doing a readback and CRC
      boot_type: The source for bootdevice (nand, sdmmc, or spi)
      checksum: The checksum of the payload (an integer)
      bus: The bus number

    Returns:
      A tuple containing:
        The script, as a string ready to use as a U-Boot boot command, with an
            embedded marker for the load address.
        The marker string, which the caller should replace with the correct
            load address as 8 hex digits, without changing its length.
    """
    replace_me = 'zsHEXYla'
    page_size = 4096
    if boot_type == 'sdmmc':
      page_size = 512
    if boot_type != 'spi':
      update = False

    cmds = [
        'setenv address       0x%s' % replace_me,
        'setenv firmware_size %#x' % payload_size,
        'setenv length        %#x' % RoundUp(payload_size, page_size),
        'setenv blocks   %#x' % (RoundUp(payload_size, page_size) / page_size),
        'setenv _crc    "crc32 -v ${address} ${firmware_size} %#08x"' %
            checksum,
        'setenv _clear  "echo Clearing RAM; mw.b     ${address} 0 ${length}"',
    ]
    if boot_type == 'nand':
      cmds.extend([
          'setenv _init   "echo Init NAND;  nand info"',
          'setenv _erase  "echo Erase NAND; nand erase            0 ${length}"',
          'setenv _write  "echo Write NAND; nand write ${address} 0 ${length}"',
          'setenv _read   "echo Read NAND;  nand read  ${address} 0 ${length}"',
      ])
    elif boot_type == 'sdmmc':
      cmds.extend([
          'setenv _init   "echo Init EMMC;  mmc rescan            0"',
          'setenv _erase  "echo Erase EMMC; "',
          'setenv _write  "echo Write EMMC; mmc write 0 ${address} 0 ' \
             '${blocks} boot1"',
          'setenv _read   "echo Read EMMC;  mmc read 0 ${address} 0 ' \
             '${blocks} boot1"',
      ])
    else:
      cmds.extend([
          'setenv _init   "echo Init SPI;   sf probe            %s"' % bus,
          'setenv _erase  "echo Erase SPI;  sf erase            0 ${length}"',
          'setenv _write  "echo Write SPI;  sf write ${address} 0 ${length}"',
          'setenv _read   "echo Read SPI;   sf read  ${address} 0 ${length}"',
          'setenv _update "echo Update SPI; sf update ${address} 0 ${length}"',
      ])

    cmds.extend([
        'echo Firmware loaded to ${address}, size ${firmware_size}, '
            'length ${length}',
        'if run _crc; then',
        'run _init',
    ])
    if update:
      cmds += ['time run _update']
    else:
      cmds += ['run _erase', 'run _write']
    if verify:
      cmds += [
        'run _clear',
        'run _read',
        'run _crc',
      ]
    else:
      cmds += ['echo Skipping verify']
    cmds.extend([
      'else',
      'echo',
      'echo "** Checksum error on load: please check download tool **"',
      'fi',
      ])
    script = '; '.join(cmds)
    return script, replace_me

  def PrepareFlasher(self, uboot, payload, update, verify, boot_type, bus):
    """Get a flasher ready for sending to the board.

    The flasher is an executable image consisting of:

      - U-Boot (u-boot.bin);
      - a special FDT to tell it what to do in the form of a run command;
      - (we could add some empty space here, in case U-Boot is not built to
          be relocatable);
      - the payload (which is a full flash image, or signed U-Boot + fdt).

    Args:
      uboot: Full path to u-boot.bin.
      payload: Full path to payload.
      update: Use faster update algorithm rather then full device erase
      verify: Verify the write by doing a readback and CRC
      boot_type: the src for bootdevice (nand, sdmmc, or spi)

    Returns:
      Filename of the flasher binary created.
    """
    fdt = self._fdt.Copy(os.path.join(self._tools.outdir, 'flasher.dtb'))
    payload_data = self._tools.ReadFile(payload)
    payload_size = os.stat(payload).st_size

    # Make sure that the checksum is not negative
    checksum = binascii.crc32(payload_data) & 0xffffffff

    script, replace_me = self._GetFlashScript(len(payload_data), update,
                                              verify, boot_type, checksum, bus)
    data = self._tools.ReadFile(uboot)
    fdt.PutString('/config', 'bootcmd', script)
    fdt_data = self._tools.ReadFile(fdt.fname)

    # Work out where to place the payload in memory. This is a chicken-and-egg
    # problem (although in case you haven't heard, it was the chicken that
    # came first), so we resolve it by replacing the string after
    # fdt.PutString has done its job.
    #
    # Correction: Technically, the egg came first. Whatever genetic mutation
    # created the new species would have been present in the egg, but not the
    # parent (since if it was in the parent, it would have been present in the
    # parent when it was an egg).
    #
    # Question: ok so who laid the egg then?
    payload_offset = len(data) + len(fdt_data)

    # NAND driver expects 4-byte alignment.  Just go whole hog and do 4K.
    alignment = 0x1000
    payload_offset = (payload_offset + alignment - 1) & ~(alignment - 1)

    load_address = self.text_base + payload_offset,
    new_str = '%08x' % load_address
    if len(replace_me) is not len(new_str):
      raise ValueError("Internal error: replacement string '%s' length does "
          "not match new string '%s'" % (replace_me, new_str))
    if len(re.findall(replace_me, fdt_data)) != 1:
      raise ValueError("Internal error: replacement string '%s' already "
          "exists in the fdt (%d matches)" % (replace_me, matches))
    fdt_data = re.sub(replace_me, new_str, fdt_data)

    # Now put it together.
    data += fdt_data
    data += "\0" * (payload_offset - len(data))
    data += payload_data
    flasher = os.path.join(self._tools.outdir, 'flasher-for-image.bin')
    self._tools.WriteFile(flasher, data)

    # Tell the user about a few things.
    self._tools.OutputSize('U-Boot', uboot)
    self._tools.OutputSize('Payload', payload)
    self._out.Notice('Payload checksum %08x' % checksum)
    self._tools.OutputSize('Flasher', flasher)
    return flasher

  def _NvidiaFlashImage(self, uboot, bct, payload):
    """Flash the image to SPI flash.

    This creates a special Flasher binary, with the image to be flashed as
    a payload. This is then sent to the board using the nvflash utility.

    Args:
      uboot: Full path to u-boot.bin.
      bct: Full path to BCT file (binary chip timings file for Nvidia SOCs).
      payload: Full path to payload.

    Returns:
      True if ok, False if failed.
    """
    # Use a Regex to pull Boot type from BCT file.
    match = re.compile('DevType\[0\] = NvBootDevType_(?P<boot>([a-zA-Z])+);')
    bct_dumped = self._tools.Run('bct_dump', [bct]).splitlines()
    boot_type = filter(match.match, bct_dumped)
    boot_type = match.match(boot_type[0]).group('boot').lower()

    flasher = self.PrepareFlasher(uboot, payload, self.update, self.verify,
                                  boot_type, 0)

    self._out.Progress('Uploading flasher image')
    args = [
      '--bct', bct,
      '--setbct',
      '--bl',  flasher,
      '--go',
      '--setentry', "%#x" % self.text_base, "%#x" % self.text_base
    ]

    # TODO(sjg): Check for existence of board - but chroot has no lsusb!
    last_err = None
    for tries in range(10):
      try:
        # TODO(sjg): Use Chromite library so we can monitor output
        self._tools.Run('nvflash', args, sudo=True)
        self._out.Notice('Flasher downloaded - please see serial output '
            'for progress.')
        return True

      except CmdError as err:
        if not self._out.stdout_is_tty:
          return False

        # Only show the error output once unless it changes.
        err = str(err)
        if not 'USB device not found' in err:
          raise CmdError('nvflash failed: %s' % err)

        if err != last_err:
          self._out.Notice(err)
          last_err = err
          self._out.Progress('Please connect USB A-A cable and do a '
              'recovery-reset', True)
        time.sleep(1)

    return False

  def _WaitForUSBDevice(self, name, vendor_id, product_id, timeout=10):
    """Wait until we see a device on the USB bus.

    Args:
      name: Board type name
      vendor_id: USB vendor ID to look for
      product_id: USB product ID to look for
      timeout: Timeout to wait in seconds

    Returns
      True if the device was found, False if we timed out.
    """
    self._out.Progress('Waiting for board to appear on USB bus')
    for tries in range(timeout * 2):
      try:
        args = ['-d', '%04x:%04x' % (vendor_id, product_id)]
        self._tools.Run('lsusb', args, sudo=True)
        self._out.Progress('Found %s board' % name)
        return True

      except CmdError as err:
        pass

      time.sleep(.5)

    return False

  def _ExynosFlashImage(self, uboot, bl1, bl2, payload):
    """Flash the image to SPI flash.

    This creates a special Flasher binary, with the image to be flashed as
    a payload. This is then sent to the board using the nvflash utility.

    Args:
      uboot: Full path to u-boot.bin.
      bl1: Full path to file containing BL1 (pre-boot).
      bl2: Full path to file containing BL2 (SPL)
      payload: Full path to payload.

    Returns:
      True if ok, False if failed.
    """
    flasher = self.PrepareFlasher(uboot, payload, self.update, self.verify,
                                  'Spi', '1:0')

    vendor_id = 0x04e8
    product_id = 0x1234

    self._out.Progress('Reseting board via servo')
    args = ['warm_reset:on', 'fw_up:on', 'pwr_button:press', 'sleep:.1',
        'warm_reset:off']
    # TODO(sjg) If the board is bricked a reset does not seem to bring it
    # back to life.
    # BUG=chromium-os:28229
    args = ['cold_reset:on', 'sleep:.2', 'cold_reset:off'] + args
    self._tools.Run('dut-control', args)
    time.sleep(2)

    self._out.Progress('Uploading flasher image')
    download_list = [
        ['bl1', 0x02021400, bl1],
        ['bl2', 0x02023400, bl2],
        ['u-boot', 0x43e00000, flasher]
        ]
    first = True
    try:
      for item in download_list:
        if not self._WaitForUSBDevice('exynos', vendor_id, product_id, 4):
          if first:
            raise CmdError('Could not find Exynos board on USB port')
          raise CmdError("Stage '%s' did not complete" % item[0])
        args = ['-a', '%#x' % item[1], '-f', item[2]]
        first = False
        self._out.Notice(item[2])
        self._out.Progress("Uploading stage '%s'" % item[0])

        # TODO(sjg): Remove this delay, once the need for it is understood.
        time.sleep(1)
        self._tools.Run('smdk-usbdl', args, sudo=True)

    finally:
      args = ['fw_up:off', 'pwr_button:release']
      self._tools.Run('dut-control', args)

    self._out.Notice('Flasher downloaded - please see serial output '
        'for progress.')
    return True

def DoWriteFirmware(output, tools, fdt, flasher, file_list, image_fname,
                    text_base=None, update=True, verify=False, dest=None):
  """A simple function to write firmware to a device.

  This creates a WriteFirmware object and uses it to write the firmware image
  to the given destination device.

  Args:
    output: cros_output object to use.
    tools: Tools object to use.
    fdt: Fdt object to use as our device tree.
    flasher: U-Boot binary to use as the flasher.
    file_list: Dictionary containing files that we might need.
    image_fname: Filename of image to write.
    text_base: U-Boot text base (base of executable image), None for default.
    update: Use faster update algorithm rather then full device erase.
    verify: Verify the write by doing a readback and CRC.
    dest: Destination device to write firmware to (usb, sd).
  """
  write = WriteFirmware(tools, fdt, output)
  if text_base:
    write.text_base = text_base
  write.update = update
  write.verify = verify
  if dest == 'usb':
    method = fdt.GetString('/chromeos-config', 'flash-method', 'tegra')
    if method == 'tegra':
      ok = write._NvidiaFlashImage(flasher, file_list['bct'], image_fname)
    elif method == 'exynos':
      ok = write._ExynosFlashImage(flasher, file_list['exynos-bl1'],
          file_list['exynos-bl2'], image_fname)
    else:
      raise CmdError("Unknown flash method '%s'" % method)
    if ok:
      output.Progress('Image uploaded - please wait for flashing to '
          'complete')
    else:
      raise CmdError('Image upload failed - please check board connection')
  elif dest == 'sd':
    pass
  else:
    raise CmdError("Unknown destination device '%s'" % dest)
