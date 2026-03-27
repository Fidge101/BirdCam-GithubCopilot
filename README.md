# BirdCam

BirdCam is a Python application for Raspberry Pi that connects to a Tapo C120 over RTSP, shows a live feed, captures timestamped frames on a schedule, and generates a contact-sheet timelapse image.

## Features

- Load all settings from a `.env` file with `python-dotenv`
- Connect to the Tapo C120 HD RTSP stream via OpenCV
- Run a live viewer with timestamp overlay
- Capture frames in a background thread while the viewer stays open
- Keep frame storage bounded by deleting the oldest captures
- Generate a contact-sheet timelapse JPEG and optional animated GIF

## Find The Camera IP

Use one of these methods on your local network:

- Check your router or access point client list for the Tapo camera hostname or MAC address
- Open the Tapo app and inspect the device details page for network information
- Scan your subnet from the Raspberry Pi with a tool such as `arp -a` or `nmap -sn 192.168.1.0/24`

## Enable RTSP On The Tapo C120

In the Tapo app:

1. Open the camera.
2. Go to `Settings > Advanced > Camera Account`.
3. Create or confirm the RTSP username and password.
4. Ensure the camera is reachable from the Raspberry Pi on the same network.

BirdCam uses the HD stream path `/stream1`. The camera also exposes `/stream2`, which is the lower-resolution sub-stream.

## RTSP Timeout Troubleshooting

If you see an OpenCV/FFmpeg message like `Stream timeout triggered after 30000 ms` and BirdCam logs `Failed to open RTSP stream`, check the following:

- Confirm RTSP is enabled in `Settings > Advanced > Camera Account` and use that camera account (not the main Tapo app login)
- Verify network reachability from the Pi: `ping CAMERA_IP` and `nc -vz CAMERA_IP 554`
- Test the stream directly with ffplay: `ffplay "rtsp://USER:PASS@CAMERA_IP:554/stream1"`
- Include `:554` explicitly in test URLs, and URL-encode special characters in passwords when needed
- Ensure camera and Pi are on the same subnet/VLAN and that port 554 is not blocked by firewall rules

## Raspberry Pi Setup

Create and activate a virtual environment, then install system and Python dependencies:

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip python3-dev libatlas-base-dev libjpeg-dev libopenjp2-7
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

If OpenCV wheels are slow or unavailable on your Pi image, `libatlas-base-dev` is commonly needed for scientific Python packages.

## Configuration

Copy the example file and edit the values for your environment:

```bash
cp .env.example .env
```

Required variables:

- `CAMERA_IP` - IP address of the Tapo C120
- `CAMERA_USER` - RTSP username configured in the Tapo app
- `CAMERA_PASS` - RTSP password configured in the Tapo app
- `CAPTURE_INTERVAL_SECONDS` - how often to save a frame
- `TIMELAPSE_OUTPUT_PATH` - JPEG contact sheet output path
- `FRAME_STORE_DIR` - directory for captured JPEG frames
- `MAX_FRAMES` - retention cap before oldest frames are deleted

## Usage

Run the live viewer:

```bash
python main.py --live
```

Run scheduled frame capture only:

```bash
python main.py --capture
```

Run the live viewer and background capture together:

```bash
python main.py --all
```

Generate the timelapse contact sheet from saved frames:

```bash
python main.py --timelapse
```

Override the number of timelapse grid columns:

```bash
python main.py --timelapse --columns 8
```

## Project Structure

- `config.py` loads and validates `.env` settings
- `camera.py` manages the OpenCV RTSP stream and reconnect logic
- `viewer.py` runs the live display loop
- `scheduler.py` captures frames in a background thread
- `timelapse.py` creates the contact-sheet output and optional GIF
- `main.py` provides the CLI entry point
