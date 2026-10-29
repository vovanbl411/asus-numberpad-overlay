# ASUS NumberPad overlay

This overlay packages the upstream Python driver and its `dev-python/python-periphery` dependency. It targets Wayland. Version 7.3.0 was manually tested on one ASUE1410:01 04F3:3230 device with the b7402 layout, Python 3.14 and Umbriel; this does not establish compatibility with other devices.

## Install

Add this repository to `/etc/portage/repos.conf/asus-numberpad-overlay.conf`, replacing the path with your checkout:

```ini
[asus-numberpad-overlay]
location = /path/to/asus-numberpad-overlay
masters = gentoo
```

Accept the appropriate unstable keywords for both packages and select a common Python target, then run:

```sh
sudo emerge --ask sys-apps/asus-numberpad-driver
```

## Device access

The user service reads the touchpad and keyboard input event devices and creates a virtual input device through `/dev/uinput`. It also needs read/write access to the touchpad's hidraw node. The driver falls back to the corresponding I²C bus if hidraw is unavailable; bus access exposes every device on that bus. Input access can expose keystrokes and uinput can inject them. Grant access only to the trusted service user and the necessary devices. Do not grant access to all hidraw or I²C nodes.

Identify the actual touchpad, keyboard and hidraw devices using `/proc/bus/input/devices` and `udevadm info --attribute-walk --name=/dev/...`. Device numbers can change. Arrange device-specific udev rules or ACLs for the selected devices and uinput; ACLs applied directly to device nodes normally disappear when devices are recreated. Rules must match stable hardware attributes for your machine. If hidraw works, I²C access is not required. Reloading rules and assigning groups or ACLs are administrator actions and are not performed by this package.

## Configure and run

Choose a layout from `/usr/share/asus-numberpad-driver/layouts`. `b7402` is the tested choice for the device above, not a default for every machine. Create a writable configuration directory and a service environment file; use an absolute path without spaces and retain the trailing slash:

```sh
mkdir -p "$HOME/.config/asus-numberpad-driver"
cat > "$HOME/.config/asus-numberpad-driver/service.conf" <<EOF2
NUMBERPAD_LAYOUT=b7402
NUMBERPAD_CONFIG_DIR=$HOME/.config/asus-numberpad-driver/
EOF2
```

The driver creates `numberpad_dev` there. Start the service from the compositor's startup configuration after the Wayland display is ready and the session environment is set. Configure the compositor to run the following shell sequence in the graphical session; the exact startup entry syntax depends on the compositor:

```sh
: "${WAYLAND_DISPLAY:?}" "${XDG_RUNTIME_DIR:?}" "${DBUS_SESSION_BUS_ADDRESS:?}"
test "${XDG_SESSION_TYPE:-}" = wayland &&
  systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR XDG_SESSION_TYPE DBUS_SESSION_BUS_ADDRESS &&
  systemctl --user start asus-numberpad-driver.service
```

Run the sequence as a shell command, such as `sh -c '…'` if the compositor expects a single command. The variables must be present in that startup process; check the user manager's environment with `systemctl --user show-environment`. The service has no enable target and is not started on installation. To stop it at session exit, configure a compositor shutdown action to run `systemctl --user stop asus-numberpad-driver.service`, or stop it manually. Check its log with `journalctl --user -u asus-numberpad-driver.service -b`.

## Remove

```sh
systemctl --user stop asus-numberpad-driver.service
sudo emerge --ask --depclean sys-apps/asus-numberpad-driver
```

Review the depclean selection. Remove the user configuration and any device access rules separately if no longer needed.
