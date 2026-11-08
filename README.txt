PrintMerged = PC Print + Phone Print (one program)

Run:    pip install -r requirements.txt   then   python app.py
Build:  pyinstaller --noconsole --onefile --add-data "static;static" app.py
        (use --icon / --name as you did before)

PC window  : your PC Print screen + a "Phone" button (QR, PIN on/off, PIN, port).
Phone      : scan the QR -> touch page (static/phone.html). Same server, same file list, same settings.
Windows firewall asks once: Allow on Private networks, or the phone cannot connect.
Saved always: page numbers, printer options, phone PIN/port, printer profiles (in %APPDATA%\PCPrint).
Never saved : file list and uploaded files (gone when you quit).

Other apps / Share:  the old Phone Print API is kept, so your other app can connect the same way:
  POST /api/login {"pin":"1234"}  (always answers ok when the PIN switch is off, keep the cookie)
  GET  /api/files
  POST /api/upload  multipart field "file"  (one file per request -> answers with that file, or 400 {"error":...})
  Files with no extension / odd names are recognised by their content (PDF or picture).

AUTO UPDATE  (top bar > Updates)
 - ON (default): a few seconds after start, and every 6 hours while the program is open, it looks on GitHub for a newer build.
   When there is one you get a Windows tray message and a question "Update now / Later" in the window. Only after
   "Update now" it downloads the new PCPrint.exe, closes, swaps the file and starts again.
   Settings, printer profiles and the phone PIN are kept (they live in %APPDATA%\PCPrint). The file list is NOT saved,
   so it is empty after an update. It never updates while a print job is running.
 - OFF: it never looks and never shows anything about updates. ("Check for updates now" still works when you click it.)
 - Only the PC window can use it: the phone page cannot start an update.
 - How it works: every build that finishes on the main branch is published as a GitHub Release  build-<number>
   (file PCPrint-windows.zip). The build writes its own number and your repository name into the program (buildinfo.py),
   so nothing needs to be set up. (Old v* tag releases are no longer made: the updater only reads build-<number> releases.)
 - Needs: a PUBLIC repository (a private one cannot be read by the program without a login), and PCPrint.exe must be in a
   writable folder (C:\Tools\PCPrint is fine, C:\Program Files is not). Details of a failed update: %APPDATA%\PCPrint\update.log
 - Repository Settings > Actions > General > Workflow permissions must allow "Read and write permissions".
 - Test the update logic: python test_updater.py

SAVE PDF  (step 2, top right: "Save PDF" and the folder button)
 - Saves every ticked page, in the same order as it prints, as ONE PDF. Page numbers, scaling, color/mono, orientation and
   everything changed with Transform (moved / resized / rotated / copied pictures, added pages) are included: it is the same
   result as the printout. Page size = the paper size chosen in Print settings (works without a printer too: A4 by default).
 - First click asks for a folder once; after that it saves straight into that folder. The folder button changes the folder,
   opens it, and lets you type a file name (empty = automatic, "<first file> merged <date time>"). An existing file is never
   overwritten: a " (2)" is added. The folder is kept in %APPDATA%\PCPrint\save.json. Only the PC window can use it.
 - Quality: 200 dpi pictures inside the PDF (text stays sharp, files stay small).

TRANSFORM:  Shift + drag = the picture(s) move dead straight (sideways, up / down or exactly diagonal). Works for moving and for
   Alt + drag copying. While Shift is held the picture stays on its page.
