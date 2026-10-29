# NeoMatrix

A Calibre plugin for bringing BOOX NeoReader reading data into your library,
and sending verified reading positions back over USB.

## Download and install

Download the latest **NeoMatrix_v*.zip** from the [releases page](https://github.com/hepkat23/NeoMatrix/releases/latest).
In Calibre, choose **Preferences → Plugins → Load plugin from file**, select
that ZIP, then restart Calibre. The GitHub source-code ZIP is not the installer.

You need Calibre 7 or newer and Android Platform Tools (ADB). Enable USB
debugging on BOOX and approve your computer on the device. No paid service,
cloud account or additional Calibre plugin is required.

## Sync

1. Save and close the books on both readers.
2. Connect and unlock BOOX, then open **NeoMatrix → Sync** in Calibre.
3. Review any proposed positions before sending them to BOOX.
4. Reopen and close a pushed book in NeoReader to load its new position.
   Until you do, that book is skipped and named in Sync details; the rest of
   your library still syncs.

The farther **verified** position wins, regardless of timestamps. If a book's
position cannot be mapped safely, NeoMatrix keeps existing positions and tells
you why. It does not guess progress from a percentage.

- EPUB: verified text positions, supported highlights, typed notes and bookmarks.
- PDF, CBZ and CBR: verified physical-page progress in both directions, using
  the same format in Calibre and BOOX. Printed PDF page labels may differ.
- Original BOOX data is archived locally; handwriting has preservation and
  viewing support, not full editable conversion into Calibre annotations.
- Device position changes and explicit cleanup operations create recovery backups.
  Normal Sync does not delete book records or ebook files.

## Public preview: compatibility limits

Hardware tests used a BOOX Go Color 7 Gen II running Android 13 and Calibre on
macOS. Other firmware and documents can use different position conventions;
unverified cases are reported rather than treated as supported.

Comic ordering currently requires the inspected NeoReader provider build and
unambiguous numeric page names in one folder. PDF pages omitted by Calibre's
viewer conversion cannot themselves be mapped. Some EPUB spatial positions
within the same section remain incomparable. Different formats or changed
reading content are not interchangeable.

Handwriting collection may require BOOX's Android backup prompt. If fresh
handwriting metadata cannot be collected, only validated preserved data is used.
Onyx cloud syncing is separate; this plugin does not manage or replace it.
The CrossPoint/X3 workflow remains EPUB-only.

Keep an independent backup of your library and device data. Automatic recovery
backups do not cover every possible firmware behaviour or cloud conflict.

## Licence and independence

Original NeoMatrix code is **GPL-3.0-only**, without warranty; see
[LICENSE.txt](LICENSE.txt) and [NOTICE.txt](NOTICE.txt).
Bundled pypdf keeps its BSD-3-Clause licence; see
[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).

NeoMatrix is an independent project, not affiliated with or endorsed by
Onyx/BOOX, calibre, OpenAI or CrossPoint. It ships no proprietary BOOX software,
books, credentials or device backups. The licence is not a promise of immunity
from legal claims.

See [README.txt](README.txt) for technical details and the full change history.

## Recent changes

- **1.4.16:** no more match prompts for files that aren't books (TXT reading
  lists, notes, exports). "Leave unmatched" is remembered, including BOOX's
  duplicate copies, and the dialog only lists books in the same format.
- **1.4.15:** the NeoMatrix window shows the real installed version (it was
  stuck saying 1.4.13).
- **1.4.14:** one pushed book you haven't reopened yet no longer stops the whole
  Sync. It's skipped and named in Sync details; everything else syncs.
- **1.4.13:** verified EPUB section order is compared before offsets inside a
  section, so a spatial cover position no longer blocks a later section.
