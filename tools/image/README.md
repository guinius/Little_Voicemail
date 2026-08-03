# Building the image

The prebuilt image is Raspberry Pi OS Lite 64-bit with Little Voicemail
already installed. Flash it, power on, open the web UI — there is no SSH step
and nothing to install.

Most people should just download it from
[Releases](https://github.com/guinius/Little_Voicemail/releases). This
directory is for building it yourself.

## Build it

```bash
sudo tools/build-image.sh --work-dir /var/tmp/lvbuild --output-dir dist
```

Needs root (`losetup`, `mount`, `chroot`), about 12 GB of scratch space, and:

```bash
sudo apt install parted e2fsprogs dosfstools zerofree xz-utils rsync curl
```

On x86 it also needs `qemu-user-static` with arm64 binfmt registered. On an
arm64 machine — a Pi 4 or 5, an Apple-silicon VM, an `ubuntu-24.04-arm` CI
runner — nothing is emulated and it is roughly four times faster.

Expect a **~1.0–1.3 GB** `.img.xz`, and roughly:

| Where | Time |
|---|---|
| arm64, 4 cores (public-repo CI runner, Pi 5, Apple-silicon VM) | 12–20 min |
| arm64, 2 cores (**private**-repo CI runner) | 30–45 min |
| x86 under qemu emulation | 45–75 min |

`xz -T0 -9` is the long pole, so core count matters more than anything else.
GitHub's `ubuntu-24.04-arm` runners give private repositories two vCPUs and
public repositories four, which is most of the difference between the first
two rows.

## Releasing

`.github/workflows/image.yml` runs the same script. Push a `v*` tag and it
builds, creates the release and attaches the `.img.xz` and its `.sha256` —
there is no release to create by hand:

```bash
git tag -a v0.2.0 -m "..." && git push origin v0.2.0
```

To try a build without minting a release, use **Run workflow** on the Actions
tab. That uploads the image as a 7-day artifact and skips the release step,
which is gated on the tag.

If the build succeeds but attaching to the release 403s, check Settings →
Actions → General → **Workflow permissions**: repositories created after
February 2023 default `GITHUB_TOKEN` to read-only.

## How it works

It customises the official image rather than building an OS from scratch.
pi-gen takes hours and drifts from upstream, and almost everything that makes
this a Raspberry Pi OS image is something we want left exactly alone.

| File | Job |
|---|---|
| `../build-image.sh` | orchestrates: fetch, grow, loop-mount, chroot, verify, clean, shrink, compress |
| `lib.sh` | mount/loop helpers and the cleanup trap |
| `chroot-setup.sh` | runs `install.sh` in image-build mode, inside the image |
| `verify-image.sh` | asserts the image is what we meant to build |
| `cleanup.sh` | strips every per-device secret, then asserts they are gone |
| `first-boot.sh` + `little-voicemail-firstboot.service` | the bits only real hardware can do |
| `lv-netctl` | the root helper the setup portal calls instead of blanket `sudo nmcli` |
| `lv-portal-dnsmasq.conf` | captive-portal DNS for the setup access point |

### Raspberry Pi Imager still works

Nothing touches `cmdline.txt`, `/usr/lib/raspberrypi-sys-mods/`, or the boot
partition's existing files, so Imager's OS customisation — hostname, WiFi,
SSH, user — applies to this image exactly as it does to the official one, and
the root filesystem still expands to fill the card on first boot.

`verify-image.sh` asserts `firstboot` is still in `cmdline.txt`, and the
builder asserts the MBR disk identifier survived the resize — `cmdline.txt`
says `root=PARTUUID=<diskid>-02`, and a `parted` that recreated the partition
instead of resizing it would produce an image that panics on boot, with no
console to see it on.

### Nothing personal is baked in

`cleanup.sh` removes and then **asserts the absence of**: the TLS certificate
and key, the Flask session key, the parent password hash, any Signal account
state, `/etc/machine-id`, SSH host keys, the builder's own WiFi credentials,
and any git credential in the checkout. A failed assertion fails the build.
Shipping one shared private key to every device is not a bug you get to fix
later.

The checkout keeps its `.git`, deliberately — `src/updater.py` needs a real
checkout for the one-click update to work.

## What this cannot tell you

Whether the overlay actually loads, whether I²C enumerates, whether audio
works. Only a real Pi with the HAT fitted can. `verify-image.sh` catches the
build failures that really happen — a `pip install` that quietly did nothing
under emulation, an overlay a newer base image no longer ships, a unit copied
but never enabled — and stops there.
