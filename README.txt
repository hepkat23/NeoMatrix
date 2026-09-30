NeoMatrix 1.2.6 — native Calibre EPUB reader integration

INSTALL AND USE
1. Close Calibre's ebook viewer before importing positions.
2. Install this ZIP with Preferences > Plugins > Load plugin from file.
3. Restart Calibre. For a connected device, use NeoMatrix > Sync BOOX. For
   already archived data, use NeoMatrix > Tools… > Integrate saved data; no
   BOOX reconnection is needed.
4. Use Tools… > Reader integration report (all books) to see the result for each book.
5. Open the EPUB from Calibre's library. Mapped highlights and typed notes are
   native viewer annotations. Mapped bookmarks appear in the viewer's bookmarks.
   A normal Sync (or Integrate saved data (all books)) asks once how to handle existing Calibre
   positions: Apply BOOX positions, Keep Calibre positions, or Cancel. Apply is the
   recommended choice when importing a test library; it overwrites mapped positions.
   Keep leaves the current Calibre position untouched for this operation.

Sync connected BOOX runs both library and reader integration automatically.
USB debugging approval now has a responsive two-minute waiting dialog and Cancel.
The separate Android handwriting-backup approval retains the previous workflow.
Cancellation or a failed collection cannot reuse the preceding successful sync.

WHAT IS MAPPED
Highlights are anchored by text that occurs uniquely within the identified EPUB
chapter, or uniquely in the book if the chapter cannot be identified. Quotes can
span formatting elements and differing whitespace. Offsets use UTF-16 for Calibre.
Typed notes accompany those highlights. Repeated quotes are skipped if ambiguous.
Bookmarks use verified chapter-start positions or uniquely located quote text.
BOOX numerical offsets are not blindly copied into Calibre.

Resume mapping supports chapter starts identified by EPUB manifest ID, and BOOX
DOM paths whose indexing convention can be calibrated from matching highlights
in that same book. Other BOOX positions are reported as unsupported. This release
does NOT promise that every book will resume correctly; no percentage-based guess
is substituted. Existing Calibre positions remain when conversion is unsupported.

The converter uses Calibre's own EPUB extraction and viewer container routines to
match the viewer's spine order and parsed documents. Source EPUBs are not modified.
PDF/CBZ/native handwriting overlay conversion is not implemented. Existing library
columns, notes, raw annotations, and handwriting archives remain available.

DATA AND FUTURE DEVICE PLUGINS
neomatrix_reading_state_v1: existing structured BOOX archive data.
neomatrix_portable_anchors_v1: verified anchors, source EPUB SHA-256, spine href,
  DOM text offset in Unicode codepoints, chapter text SHA-256, context, and Calibre CFI.
  This is an interchange representation; device plugins must still translate their
  own formats. It is not an X3 progress.bin file or a BOOX write-back implementation.
neomatrix_reader_report_v1: mapped annotation count, resume mapping/application
  status, and reasons for skipped items or failed native integration.

Native annotations use deterministic UUIDs and BOOX timestamps. Calibre's merge API
preserves unrelated annotations and prefers newer edits. Repeated import does not
create duplicate highlights. Bookmarks have BOOX-labelled stable titles. A normal
Keep choice preserves an existing Calibre resume position; Apply deliberately
overwrites the mapped resume position for the current operation.
Deleted annotation propagation and two-way conflict handling are not implemented.
All original extraction/handwriting safeguards and read-only BOOX behavior remain.

VALIDATION
27 automated checks passed (3 GUI checks skipped because PyQt6 is unavailable in the
build environment): existing saved-archive integration, conflict checks,
column rollback, escaping, Unicode/UTF-16 offsets, multi-element highlights,
whitespace, ambiguity rejection, calibrated versus unsupported positions,
stable IDs/repeat imports, raw source preservation, and upstream Calibre merge
behavior preserving newer positions and unrelated notes. Python compilation and
ZIP integrity checks passed.

The desktop Calibre GUI, USB approval dialog and actual device reading positions
depend on the local Calibre and BOOX environment. Mapping tests use synthetic
chapter fixtures; consult the per-book report for book-specific results and retain
the original archives.

1.2.2: Device collection and reader integration run on background threads.
The main window shows collection stages; backup approval remains on BOOX.
Removed countdown and intermediate success/backup confirmation popups.
A failed backup is validated against preserved handwriting before archiving.

1.2.2: Reject scalar JSON in location fields; add explicit Use BOOX reading position
action for books selected in the main Calibre library. Preserves displaced viewer
position as a bookmark. Close the viewer before applying and reopen afterwards.
Unmapped positions remain unchanged. Export selected book for diagnosis creates
a local ZIP with the complete EPUB and saved BOOX state; it uploads nothing.

1.2.3: Persist a verified resume position to Calibre's per-book viewer sidecar,
which is what the desktop ebook viewer actually reads on startup. The EPUB is
never modified. The library's last-read position index is updated as a secondary
record; existing viewer annotations remain intact. A newer local viewer position
is preserved by the Keep choice unless an Apply choice (or the explicit Tools
action) is made.

1.2.4: Simplified the main window to two primary controls: Sync BOOX and Tools…
Browse, integrate, apply position, report, and diagnosis export remain available
from the Tools menu.

1.2.5: Sync and Integrate saved data ask once per operation whether to apply BOOX
reading positions, keep existing Calibre positions, or cancel. Apply is the default
recommendation for initial imports and test libraries; explicit position application
still preserves the displaced Calibre position as a bookmark where supported.

1.2.6: When a BOOX DOM path cannot be resolved directly, use an exactly matching
unique bookmark quote as a verified resume anchor. This supports NeoReader EPUBs
whose position paths do not match Calibre's DOM indexing without guessing from
percentage progress.
