NeoMatrix 1.4.5 — BOOX reading data in Calibre

INSTALL
Install the plugin ZIP in Calibre: Preferences > Plugins > Load plugin from file.
Restart Calibre. Install Android Platform Tools (ADB), enable USB debugging on
BOOX, and approve the computer on the device. The first Sync may create BOOX
columns and ask for one Calibre restart. BOOX may separately ask you to approve
an Android backup when collecting handwriting metadata.

EVERYDAY USE
Save and close books in both readers. Connect and unlock BOOX, then click Sync.
NeoMatrix reads the device, imports changed notes and reading data, and compares
reading positions in both places. Up-to-date books are not rewritten.

When newer Calibre positions can safely be sent to BOOX, Sync shows the book
names and asks: Sync both ways, Read BOOX only, or Cancel. No position is sent
without that choice. Backups are saved automatically on your computer before
any device update. Reopen and close each pushed book in NeoReader afterward;
this loads the new location and refreshes the BOOX page percentage.

Newer Calibre positions are kept even if a push is declined or unsupported.
If both readers changed since the last pull, the position is kept and the
conflict is reported. No position is guessed from a percentage.

WHAT WORKS
- BOOX to Calibre: supported EPUB highlights, typed notes, bookmarks and verified
  resume locations become native Calibre reader data. Reading progress uses
  Calibre's standard 0–1 fraction and the BOOX percentage column.
- Calibre to BOOX: on Android 12 or newer, this first push release supports EPUB resume locations already
  observed and verified in saved BOOX data, such as mapped BOOX bookmarks. An
  arbitrary new Calibre location is skipped if no verified BOOX position exists.
  Highlights, typed notes and handwriting are not pushed to BOOX yet.
- Original BOOX annotations, metadata and handwriting files are archived locally.
  Handwriting can be viewed in NeoMatrix; native editable handwriting overlays
  and PDF/CBZ reader-location conversion are not implemented.

SAFETY AND COMPATIBILITY
Push identifies the exact BOOX copy by document ID, record ID and path. Separate
volumes stay separate. EPUB reading resources and structure must match the
verified Calibre archive. Calibre bookmarks, bibliographic metadata, ZIP packing
and manifest order may differ; text, images, styles, reading order, manifest IDs
and layout metadata must match. Encrypted packages retain all package metadata.
The exact BOOX file checksum is checked again before update or restore;
BOOX's metadata MD5 tag is only an identity value.
A changed file, changed device state or ambiguous position stops the push.
The original metadata is backed up first, the update is conditional, and device
readback must match. A repeat push to the same location performs no write.

A pushed book cannot import an old BOOX page fraction before NeoReader refreshes
it. Ordinary imports retain unrelated annotations and newer Calibre positions.
No ebook file is modified. Only the exported Android metadata provider is used;
no rooting, firmware modification or proprietary source code is required.

MORE
Saved reading data: browse local reading and handwriting archives.
Free up BOOX storage: optional review of verified orphaned reader files. Scan
makes no changes. Removal requires a separate choice and a verified backup.
Books still present on internal memory/SD cards, Kindle CR! downloads, tiny caches,
shared databases and uncertain data are protected. Cleanup does not run in Sync.
Restore a reading-position backup: restores the old position only on the same
BOOX and exact book, provided there has been no newer reading since the push.
Advanced: manual imports, position overrides, reports and local diagnosis export.
Diagnosis exports contain private book/reading data; nothing is uploaded by the
plugin. The release ZIP excludes device snapshots, books, backups and diagnostics.

VALIDATION
Version 1.4.2 preserves NeoReader document-root and whitespace paths separately
from Calibre CFI paths. Observed highlight quotes verify the path convention
before resume conversion; missing or inconsistent evidence remains unsupported.

Version 1.4.1 was checked against actual Calibre and BOOX copies of two EPUBs
whose only differences were metadata and Calibre bookmarks. Reading resources,
chapter identities and converted anchors matched. A third EPUB with a changed
image remained blocked. Fourteen regression tests cover these comparisons,
incremental archive checks and checksum guards for push, backup and restore.

The first device push was checked on an original synthetic two-chapter EPUB on
BOOX Go Color 7 Gen II / Android 13. NeoReader reopened at the pushed first chapter
and changed its progress from 11/20 to 1/20. Repeat push was unchanged; other books,
bookmarks, annotations and EPUB bytes were unchanged. Firmware/format support
varies, so unsupported positions are reported and retained.

Developer regression tests cover incremental writes, separate volumes, duplicate
copies, missing paths, cleanup protection, position mapping, content checks,
backups, conditional updates, restore guards, and the main Sync choices.

NeoMatrix is an independent compatibility tool. No BOOX firmware, third-party
books or device-private diagnostics are distributed with it.

MAPPING UPDATES
After a reading-position mapper update, Sync rechecks existing EPUB archives
once even when BOOX data has not changed. Remaining unsupported positions are
listed in the sync summary, including on repeat syncs. No page turn is needed.

PROGRESS CONFLICTS
Automatic sync retains the farthest verified EPUB location across BOOX and all
Calibre last-read sources, regardless of timestamps. Uncomparable locations are
preserved and reported. Tools > Use BOOX reading position is an explicit override.
The EPUB CFI resolver is shared with the CrossPoint SD plugin in this repository
family so both plugins compare the same spine/text ordering.

1.4.5: Verify additional BOOX document-root path conventions using each book's
highlight quotes. Sync details distinguish image-only cover/title-page starts,
BOOX PDF versus Calibre EPUB copies, and unresolved text locations. Existing
positions are preserved whenever a safe comparison is unavailable.
