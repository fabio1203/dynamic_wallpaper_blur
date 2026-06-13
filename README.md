# Dynamic Wallpaper Blur

A Python script for Windows that dynamically blurs your desktop wallpaper when application windows are open, and smoothly unblurs it back to normal when you return to an empty desktop. 

## ✨ Features
* Multi-Display Support: Independently detects windows on multiple monitors and blurs the respective screens.
* Smooth Animations: Features fade-in and fade-out transitions between the sharp and blurred states.
* Highly Customizable: Finetune blur strength, fade speed, and animation framerates directly within the script.
* Non-Intrusive: Injects directly into the Windows desktop layer (behind your desktop icons).

## ⚠️ Known Issues
* Wallpaper Changes: The script currently breaks or behaves unpredictably if you change your Windows wallpaper while the script is running. (If you change your wallpaper, simply restart the script).
* Misalignment: Sometimes, the script can cause minor misalignment of the wallpaper when transitioning to the blurred state depending on your Windows scaling and Fit/Fill settings.

## 🛠️ Installation & Setup

1. Prerequisites
Ensure you have [Python 3.x](https://www.python.org/downloads/) installed on your system. During installation, make sure to check the box that says "Add Python to PATH".

2. Choose a Permanent Location
The location of the script doesn't matter, but it **must sit somewhere where it can stay the same forever** without being accidentally moved or deleted.
💡 Recommendation: Create a folder named `Scripts` on your Desktop (or in your Documents) and place `dynamic_wallpaper_blur.py` inside it.

4. Install Dependencies
Open your terminal (Command Prompt or PowerShell), navigate to the folder where you saved the script, and run:
pip install -r requirements.txt
