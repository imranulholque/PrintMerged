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
