# Changelog

## [Unreleased]

### Added

- Prebuilt add-on images, so installs and updates no longer build on your machine.
- A dev add-on tracking `main`, for testing unreleased changes.
- Add-on documentation, including how to set up the emailed code.

### Changed

- Choose one email code source, `gmail`, `imap` or `manual`, on the add-on's Configuration tab. It no longer falls back from Gmail to IMAP. **If you use IMAP, select `imap` and enter the sensor there**, because the integration no longer has that field.
- The add-on now runs on `amd64` and `aarch64` only, because its browser isn't available for the others.

### Fixed

- The add-on's Gmail page gave the wrong instructions for creating the Google client.
- The add-on's code page said the code came by SMS, not email.

## [1.1.0]

### Changed

- The API key is no longer built in: setup asks for it and explains how to find it.
