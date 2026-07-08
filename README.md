# puppisctl

Open-source configuration tool for the PrismXR Puppis S1.

This tool talks directly to the Puppis S1 over its local TCP configuration protocol,
so basic configuration can be done without PrismXR Desktop.

## Features

- Read device status
- Read 5 GHz and 2.4 GHz hotspot settings
- Change SSID/password/channel
- Read LAN/DHCP/device information
- Raw API call tab for testing discovered commands

## Requirements

- Python 3
- Tkinter
- Puppis S1 connected over its USB/network interface

No third-party Python packages are required.

## Usage

```bash
python main.py
