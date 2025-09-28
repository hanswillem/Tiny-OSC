# Tiny OSC

A lightweight Blender add-on for receiving OSC messages and mapping them directly to Blender properties via data paths. Designed with TouchDesigner in mind, but probably, maybe, compatible with Max/MSP and other OSC-capable tools.

## Installation & Setup
1. Download `tiny_osc.py`, and install through Blender's preferences.
2. Find the panel under **View3D → Sidebar → TD Osc**.
3. Set the **Network Address** (default `localhost`) and **Port** (default `10000`), and press **Listen** to begin listening.

The add-on applies incoming OSC values continuously at ~60 Hz. Received values are shown at the bottom in real time for quick feedback.

## Making Mappings
- For each mapping:  
  - Enter the **OSC Address**. In TouchDesigner this is the channel name being sent over OSC
  - Enter the **Blender Data Path**. The easiest way is to right-click a property in Blender and choose **Copy Full Data Path**.
- Values are converted automatically to the right type: floats → ints or booleans where required.  
- You can target most Blender properties.  

## Recording Keyframes
- Place the playhead anywhere and click **Record Keyframes** — Blender will start playing and inserting keys.
- While recording, animation channels are muted to avoid Blender fighting between keyframes and OSC values. They are unmuted automatically when recording stops or when listening is stopped.  
- The resulting curves can look a bit wonky, but wonky is good.
