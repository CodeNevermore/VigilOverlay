PresentMon 2.5.1 bundled runtime
================================

This directory is the single ownership, licensing, staging, and runtime location
for Vigil's third-party PresentMon dependency.

Runtime file:

  bin/PresentMon-2.5.1-x64.exe

Expected SHA-256:

  9bec3083069f58f911e6a512f4806db51a27bd096103087bc1d05ef54c80a191

Release builders download only the pinned official GitHub asset when the binary
is not already staged, enforce a bounded download size, verify this SHA-256, and
place it in bin/. Vigil never downloads, replaces, or updates PresentMon while
the application is running.

LICENSE.txt is PresentMon's upstream MIT license. THIRD_PARTY.txt preserves the
upstream v2.5.1 third-party notices. NOTICE.txt records Vigil's integration and
packaging details. These files must remain with every distributed copy of the
collector.
