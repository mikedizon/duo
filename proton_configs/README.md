# proton_configs/

A place to keep the WireGuard configuration files (`.conf`) you download from your Proton VPN account.

**These files contain private keys. They are never committed:** `.gitignore` excludes everything in this folder except this README. Don't share them, paste them anywhere, or copy them into other folders of the project.

## How they're used

The monitor doesn't read this folder. It uses the tunnels **imported into the WireGuard app** (see "VPN tunnels" in the main [README](../README.md)). This folder is just a tidy place to keep the downloaded originals, for example to re-import them on another Mac.

1. In your Proton VPN account, create and download one WireGuard configuration per server you want to use. The free plan works.
2. Save each file here, e.g. `proton_configs/88.conf`.
3. Check the key: open the file and make sure the `PrivateKey = ` line is a long string, not `*****`. A file with `*****` can't connect; download it again.
4. Import the files into the WireGuard app (*Import Tunnel(s) from File*).
5. Run `python3 iphone_monitor.py --vpn-list` to see the tunnels the monitor will use.

## Notes

- Every tunnel in the WireGuard app is used. Delete a tunnel there to stop using it; deleting its file here changes nothing.
- A tunnel that keeps failing to connect or pass traffic can usually be fixed by downloading a fresh configuration for that server.
