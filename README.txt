PrintMerged = PC Print + Phone Print (one program)
Made by Imranul Hoque Sabbir.

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
 - Quality: pages are drawn at 300 dpi at least (up to 600 dpi when a picture has more detail). Flat pages (text,
   drawings) are stored lossless, photos as JPEG quality 98 with full colour detail. Files are bigger than before.

TRANSFORM:  Shift + drag = the picture(s) move dead straight (sideways, up / down or exactly diagonal). Works for moving and for
   Alt + drag copying. While Shift is held the picture stays on its page.


PERSPECTIVE CROP  (Transform > "✂ Perspective crop", or press C with one picture selected)
 - Like Photoshop's Perspective Crop: a window opens with the whole picture and 4 corner handles. Pull each corner onto a corner of the
   area you want (a skewed photo of a paper, a sign, a screen...). The edge handles move a whole edge, dragging inside moves the shape.
   Magnets: the page border and the straight line of the next corner (hold Alt to switch the magnets off). While a corner moves, a
   magnifier shows it 3x. The arrow keys move the chosen corner (Shift = 10 px). Enter = apply, Esc = cancel.
 - Lossless: the area is cut from the ORIGINAL file, not from the screen copy. A straight (rectangle) crop copies the pixels exactly,
   nothing is resampled. A slanted crop is straightened once (bicubic) into a picture as big as its longest sides, so no side is
   made smaller. PDF pages are drawn again at the needed size (up to 1200 dpi), so text stays sharp. Print and Save PDF use the same.
 - After Apply the picture fits inside the box it had before (same centre); then move / resize / rotate / copy it as usual.
   Open the tool again to change the corners, or press "Remove crop" to get the whole picture back. Undo / redo (Ctrl+Z / Ctrl+Y) work.

WINDOW SIZE / FEEDBACK
 The window can be any size (min 480 x 400): the top bar shortens its labels, the side panels slide away below 1100 px
 (open them with the buttons: they open on top of the page), the Transform strip wraps instead of cutting buttons off.
 While you drag / copy / resize in Transform, a small read-out follows the mouse (Move / Copy / Resize with the numbers), the picture lifts,
 the page it will land on says 'Move to page N', and rotate / center / copy / paste / delete / undo show a short confirmation.


SHARP / FAST  (small coloured button in the top bar, next to the zoom buttons)
 - Sharp (default): the screen shows pictures and PDFs in full original quality (JPEG 95, full colour detail, more pixels).
 - Fast: lighter pictures on screen, quicker to open and scroll. Use it for a big file list, then switch back.
 - It changes the DISPLAY only. Print, Save PDF and your original files are never touched by it. The choice is remembered.
 - Printing now draws each page at the printer's resolution, up to 600 dpi (was 300).
