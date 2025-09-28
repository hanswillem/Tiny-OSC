# Tiny OSC

A lightweight Blender add-on for receiving OSC messages and mapping them directly to Blender properties. Designed with TouchDesigner in mind, but probably, maybe, compatible with Max/MSP and other OSC-capable tools.

## Installation & Setup
1. Download `tiny_osc.py`, and install through Blender's preferences.
2. Find the panel in the **N-Panel** of the viewport.
3. Set the **Network Address** (default `localhost`) and **Port** (default `10000`), and press **Listen** to begin listening.

The add-on applies incoming OSC values continuously at ~60 Hz. Received values are shown at the bottom in real time for quick feedback.

## Making Mappings
- Click on the + button to add mappings.
- Enter the **Address**. In TouchDesigner this is the channel name being sent over OSC.
- Enter the **Blender Data Path**. The easiest way is to right-click a property in Blender and choose **Copy Full Data Path**.

You can target most Blender properties. Just try it.

## Recording Keyframes
- Place the playhead anywhere and click **Record Keyframes** — Blender will start playing and inserting keys.

The resulting curves can look a bit wonky, but wonky is good.
