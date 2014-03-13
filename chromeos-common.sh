# Copyright (c) 2010 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
#
# This contains common constants and functions for installer scripts. This must
# evaluate properly for both /bin/bash and /bin/sh, since it's used both to
# create the initial image at compile time and to install or upgrade a running
# image.

# The GPT tables describe things in terms of 512-byte sectors, but some
# filesystems prefer 4096-byte blocks. These functions help with alignment
# issues.

# This returns the size of a file or device in 512-byte sectors, rounded up if
# needed.
# Invoke as: subshell
# Args: FILENAME
# Return: whole number of sectors needed to fully contain FILENAME
numsectors() {
  if [ -b "${1}" ]; then
    dev=${1##*/}
    if [ -e /sys/block/$dev/size ]; then
      cat /sys/block/$dev/size
    else
      part=${1##*/}
      block=$(get_block_dev_from_partition_dev "${1}")
      block=${block##*/}
      cat /sys/block/$block/$part/size
    fi
  else
    local bytes=$(stat -c%s "$1")
    local sectors=$(( $bytes / 512 ))
    local rem=$(( $bytes % 512 ))
    if [ $rem -ne 0 ]; then
      sectors=$(( $sectors + 1 ))
    fi
    echo $sectors
  fi
}

# Round a number of 512-byte sectors up to an integral number of 2Mb
# blocks. Divisor is 2 * 1024 * 1024 / 512 == 4096.
# Invoke as: subshell
# Args: SECTORS
# Return: Next largest multiple-of-8 sectors (ex: 4->8, 33->40, 32->32)
roundup() {
  local num=$1
  local div=${2:-4096}
  local rem=$(( $num % $div ))

  if [ $rem -ne 0 ]; then
    num=$(($num + $div - $rem))
  fi
  echo $num
}

# Truncate a number of 512-byte sectors down to an integral number of 2Mb
# blocks. Divisor is 2 * 1024 * 1024 / 512 == 4096.
# Invoke as: subshell
# Args: SECTORS
# Return: Next smallest multiple-of-8 sectors (ex: 4->0, 33->32, 32->32)
rounddown() {
  local num=$1
  local div=${2:-4096}
  local rem=$(( $num % $div ))

  if [ $rem -ne 0 ]; then
    num=$(($num - $rem))
  fi
  echo $num
}

# Locate the cgpt tool. It should already be installed in the build chroot,
# but some of these functions may be invoked outside the chroot (by
# image_to_usb or similar), so we need to find it.
GPT=""

locate_gpt() {
  if [ -z "$GPT" ]; then
    if [ -x "${DEFAULT_CHROOT_DIR:-}/usr/bin/cgpt" ]; then
      GPT="${DEFAULT_CHROOT_DIR:-}/usr/bin/cgpt"
    else
      GPT=$(which cgpt 2>/dev/null) || /bin/true
      if [ -z "$GPT" ]; then
        echo "can't find cgpt tool" 1>&2
        exit 1
      fi
    fi
  fi
}

# Read GPT table to find the starting location of a specific partition.
# Invoke as: subshell
# Args: DEVICE PARTNUM
# Returns: offset (in sectors) of partition PARTNUM
partoffset() {
  sudo $GPT show -b -i $2 $1
}

# Read GPT table to find the size of a specific partition.
# Invoke as: subshell
# Args: DEVICE PARTNUM
# Returns: size (in sectors) of partition PARTNUM
partsize() {
  sudo $GPT show -s -i $2 $1
}

# Read GPT table to find the partition number of a label. 
# Invoke as: subshell
# Args: DEVICE LABEL
# Returns: partition number of LABEL
partnum() {
  sudo $GPT show $1 | grep "Label: \"$2\"" | awk '{print $3}' 
}

# Extract the whole disk block device from the partition device.
# This works for /dev/sda3 (-> /dev/sda) as well as /dev/mmcblk0p2
# (-> /dev/mmcblk0).
get_block_dev_from_partition_dev() {
  local partition=$1
  if ! (expr match "$partition" ".*[0-9]$" >/dev/null) ; then
    echo "Invalid partition name: $partition" >&2
    exit 1
  fi
  # Removes any trailing digits.
  local block=$(echo "$partition" | sed -e 's/[0-9]*$//')
  # If needed, strip the trailing 'p'.
  if (expr match "$block" ".*[0-9]p$" >/dev/null); then
    echo "${block%p}"
  else
    echo "$block"
  fi
}

# Extract the partition number from the partition device.
# This works for /dev/sda3 (-> 3) as well as /dev/mmcblk0p2 (-> 2).
get_partition_number() {
  local partition=$1
  if ! (expr match "$partition" ".*[0-9]$" >/dev/null) ; then
    echo "Invalid partition name: $partition" >&2
    exit 1
  fi
  # Extract the last digit.
  echo "$partition" | sed -e 's/^.*\([0-9]\)$/\1/'
}

# Construct a partition device name from a whole disk block device and a
# partition number.
# This works for [/dev/sda, 3] (-> /dev/sda3) as well as [/dev/mmcblk0, 2]
# (-> /dev/mmcblk0p2).
make_partition_dev() {
  local block=$1
  local num=$2
  # If the disk block device ends with a number, we add a 'p' before the
  # partition number.
  if (expr match "$block" ".*[0-9]$" >/dev/null) ; then
    echo "${block}p${num}"
  else
    echo "${block}${num}"
  fi
}

# Find the uuid for a (disk, partnum) pair (e.g., ("/dev/sda", 3))
part_index_to_uuid() {
  local dev="$1"
  local idx="$2"

  sudo $GPT show -i "$idx" -u "$dev"
}

list_usb_disks() {
  local sd
  for sd in /sys/block/sd*; do
    if readlink -f ${sd}/device | grep -q usb &&
      [ "$(cat ${sd}/removable)" = 1 ]; then
      echo ${sd##*/}
    fi
  done
}

list_mmc_disks() {
  local mmc
  for mmc in /sys/block/mmcblk*; do
    if readlink -f ${mmc}/device | grep -q mmc; then
      echo ${mmc##*/}
    fi
  done
}

get_disk_info() {
  # look for a "given" file somewhere in the path upwards from the device
  local dev_path=/sys/block/${1}/device
  while [ -d "${dev_path}" -a "${dev_path}" != "/sys" ]; do
    if [ -f "${dev_path}/${2}" ]; then
      cat "${dev_path}/${2}"
      return
    fi
    dev_path=$(readlink -f ${dev_path}/..)
  done
  echo '[Unknown]'
}

legacy_offset_size_export() {
  # Exports all the variables that install_gpt did previously.
  # This should disappear eventually, but it's here to make existing
  # code work for now.

  NUM_STATEFUL=$(partnum $1 STATE)
  NUM_ROOTFS_A=$(partnum $1 ROOT-A)
  NUM_ROOTFS_B=$(partnum $1 ROOT-B)
  NUM_OEM=$(partnum $1 OEM)
  NUM_ESP=$(partnum $1 EFI-SYSTEM)

  START_STATEFUL=$(partoffset $1 ${NUM_STATEFUL})
  START_ROOTFS_A=$(partoffset $1 ${NUM_ROOTFS_A})
  START_ROOTFS_B=$(partoffset $1 ${NUM_ROOTFS_B})
  START_OEM=$(partoffset $1 ${NUM_OEM})
  START_ESP=$(partoffset $1 ${NUM_ESP})

  NUM_STATEFUL_SECTORS=$(partsize $1 ${NUM_STATEFUL})
  NUM_ROOTFS_SECTORS=$(partsize $1 ${NUM_ROOTFS_A})
  NUM_OEM_SECTORS=$(partsize $1 ${NUM_OEM})
  NUM_ESP_SECTORS=$(partsize $1 ${NUM_ESP})

  STATEFUL_IMG_SECTORS=$(partsize $1 ${NUM_STATEFUL})
  ROOTFS_IMG_SECTORS=$(partsize $1 ${NUM_ROOTFS_A})
  OEM_IMG_SECTORS=$(partsize $1 ${NUM_OEM})
  ESP_IMG_SECTORS=$(partsize $1 ${NUM_ESP})
}

install_hybrid_mbr() {
  # Creates a hybrid MBR which points the MBR partition 1 to GPT
  # partition 12 (ESP). This is useful on ARM boards that boot
  # from MBR formatted disks only
  info "Creating hybrid MBR"
  locate_gpt
  legacy_offset_size_export ${1}
  local start_esp=$(partoffset "$1" ${NUM_ESP})
  local num_esp_sectors=$(partsize "$1" ${NUM_ESP})
  sudo sfdisk "${1}" <<EOF
unit: sectors

disk1 : start=   $start_esp, size=    $num_esp_sectors, Id= c, bootable
disk2 : start=   1, size=    1, Id= ee
EOF
}
