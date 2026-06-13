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
   > 💡 **Recommendation:** Create a folder named `Scripts` on your Desktop (or in your Documents) and place `dynamic_wallpaper_blur.py` inside it.

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
     pythonw.exe "%USERPROFILE%\Desktop\Scripts\dynamic_wallpaper_blur.py"
     ```
   > <img width="707" height="590" alt="Screenshot 2026-06-13 214550" src="https://github.com/user-attachments/assets/6558e683-6ba4-4084-8046-a4128c8327b1" />
   * **Step 4:** Click Next, name the shortcut "Dynamic Wallpaper", and click Finish.
   > <img width="704" height="590" alt="Screenshot 2026-06-13 214704" src="https://github.com/user-attachments/assets/9449a497-1318-4012-af2f-02afd5ea6afb" />



## ⚠️ Known Issues
* **Wallpaper Changes:** The script currently breaks or behaves unpredictably if you change your Windows wallpaper while the script is running. (If you change your wallpaper, simply restart the script).
* **Misalignment:** Sometimes, the script can cause minor misalignment of the wallpaper when transitioning to the blurred state depending on your Windows scaling and Fit/Fill settings.
