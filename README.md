# Dynamic Wallpaper Blur on Windows

A Python script for Windows that dynamically blurs your desktop wallpaper when application windows are open, and smoothly unblurs it back to normal when you return to an empty desktop. 

## ✨ Features
* **Multi-Display Support:** Independently detects windows on multiple monitors and blurs the respective screens.
* **Smooth Animations:** Features fade-in and fade-out transitions between the sharp and blurred states.
* **Highly Customizable:** Finetune blur strength, fade speed, and animation framerates directly within the script.
* **Non-Intrusive:** Injects directly into the Windows desktop layer (behind your desktop icons).
* **Performance Friendly:** Uses pre-rendered blurred wallpaper assets and lightweight alpha blending for minimal CPU/GPU overhead.

## 🛠️ Installation & Setup

1. **Prerequisites**
   Ensure you have [Python 3.x](https://www.python.org/downloads/) installed on your system. During installation, make sure to check the box that says "Add Python to PATH".

2. **Choose a Permanent Location**
   The location of the script doesn't matter, but it **must sit somewhere where it can stay the same forever** without being accidentally moved or deleted. 
   > 💡 **Recommendation:** Create a folder named `Scripts` on your Desktop (or in your Documents) and place `dynamic_wallpaper_blur.pyw` inside it.

3. **Install Dependencies**
   Open your terminal (Command Prompt or PowerShell), navigate to the folder where you saved the script, and run (only needs to be done once):
```bash
   pip install Pillow pywin32 comtypes
```
4. 🚀 **How to Autostart on Windows Boot (Silently)**
   To have this run automatically in the background every time you turn on your PC without leaving an annoying black terminal window open, follow these steps:
   
   * **Step 1:** Press `Win + R` on your keyboard to open the Run dialog. Type `shell:startup` and press Enter. This opens your Windows Startup folder.
   > <img width="456" height="272" alt="Screenshot 2026-06-13 214741" src="https://github.com/user-attachments/assets/c5728ca5-473a-48ed-9383-a8e7a09a7efd" />
   * **Step 2:** Right-click anywhere in the empty space of the Startup folder and select **New > Shortcut**.
   > <img width="1111" height="925" alt="Screenshot 2026-06-13 214833" src="https://github.com/user-attachments/assets/a5d9a2cb-5920-4536-8a05-8c5cdbc8861e" />
   * **Step 3:** In the location box, type `pythonw.exe` (which runs Python silently) followed by a space, and then the path to your script in quotes. For example:
    ```text
     pythonw.exe "%USERPROFILE%\Desktop\Scripts\dynamic_wallpaper_blur.pyw"
     ```
   > <img width="707" height="590" alt="Screenshot 2026-06-13 214550" src="https://github.com/user-attachments/assets/6558e683-6ba4-4084-8046-a4128c8327b1" />
   * **Step 4:** Click Next, name the shortcut "Dynamic Wallpaper", and click Finish.
   > <img width="704" height="590" alt="Screenshot 2026-06-13 214704" src="https://github.com/user-attachments/assets/9449a497-1318-4012-af2f-02afd5ea6afb" />

## ⚙️ Customization & Example Images
You can tweak how the blur looks and feels by opening dynamic_wallpaper_blur.pyw in any text editor and changing the variables at the very top:

* BLUR_STRENGTH = 4 (Increase for heavier blur, decrease for lighter blur)
* FADE_SPEED = 15 (Opacity shift per frame. Higher = faster fade)
* TIMER_INTERVAL = 15 (Animation frame step interval in milliseconds)

><img width="2559" height="1439" alt="Screenshot 2026-06-13 230105" src="https://github.com/user-attachments/assets/ec6e5dae-5037-4b87-9738-5026f4de78ea" />
><img width="2559" height="1439" alt="Screenshot 2026-06-13 230211" src="https://github.com/user-attachments/assets/43b1dddf-5932-4f94-b30b-512c3d448cfb" />


## 📋 Requirements
* **Windows 10 build 1803 (April 2018 Update) or later.** The script uses Per-Monitor DPI Awareness V2 and mixed-DPI hosting to render the overlay in the same pixel grid as Explorer's wallpaper. Older builds fall back to less accurate DPI modes and may show minor misalignment on scaled displays.

## 🪵 Troubleshooting
If the overlay ever gets into a weird state (nothing shows, wrong wallpaper, misalignment), check `%TEMP%\dynamic_wallpaper_blur.log`. The script logs startup, wallpaper detection events, sleep/wake rebuilds, and every exception with a full traceback — helpful when running under `pythonw.exe` where there is no console.
