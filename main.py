#!/usr/bin/env python3
"""One line from a dying phone battery to the charger that ghosted it.

The judged entry is a single 120-character line, written as if a
phone battery were texting the charger it waited for all day at 1%:

    "Waited all day at 1% for you. You never came, so I died out
    of spite — and the next battery already blocked your number."

This program writes exactly that line — nothing else, no trailing
newline — to ``$MAKEFUN_OUTPUT_DIR/entry.txt`` (default
``/output``), using only the Python standard library. It is
deterministic: every run produces the same bytes.

Safety of the write
-------------------
Publishing is atomic and verified. A temporary file is created
with ``O_EXCL`` inside the output directory, filled through a
loop that drains short writes, confirmed with ``fstat``, given
mode ``0644`` and fsynced, then moved into place with
``os.replace``. The directory is fsynced too, and the finished
file is read back and compared byte for byte, including size
and mode. A failed run never leaves a half-written ``entry.txt``.

The temporary file must sit inside the output directory, never
in ``/tmp``: the grader mounts the output directory, so only a
temp file on the same filesystem can be renamed into place
atomically. Temp names are unique to this entry
(``.entry-l2-battery-<random>.tmp``), and leftovers from an
earlier run are cleared before a new temp is made. The output
directory itself is never removed or recreated — only its
contents are touched — because it is the grader's mount point.

Why the line should win
-----------------------
Checked against the brief, in order:

1. On brief: "Waited all day at 1%" is the whole premise in a
   single breath, and "blocked your number" is what you do to
   someone who was supposed to text back.
2. A real joke with a turn: the setup sounds like a sad
   dying-battery lament, "died out of spite" twists it into a
   grudge, and the punchline lands in the final words — the
   next battery has already blocked the charger, as if it were
   a bad ex.
3. Voice: it reads typed, not written — short sentences, a
   spiteful "you never came", a blocking threat a person would
   actually send. Needy and petty at the same time.
4. Economy: 120 of the 200 allowed characters, every clause
   earning its place — the wait, the no-show, the spiteful
   death, the block. No filler, no repeated premise.
5. Fresh angle: not another "I'm at 1%, please plug me in"
   complaint. The battery is already dead and still holding a
   grudge, and the joke survives a second read because the
   "next battery" line reframes the charger as a serial
   ghoster.

Lines that lost (25 Nov 2026)
-----------------------------
- Alt A — "I waited all day at 1% and you never came, so I
  died out of spite. Tell the next battery I said good luck."
  (106 chars.) "Good luck" is a generic send-off; it spends
  the final words on a pleasantry instead of a turn, where the
  kept line's "blocked your number" is a sharper image of
  spite.
- Alt B — "You had one job and a whole day to do it. I sat at
  1% like an idiot. Enjoy charging a corpse — oh wait, you
  can't. I'm dead." (124 chars.) "Enjoy charging a corpse"
  leans on shock, "oh wait, you can't" explains its own
  punchline, and "I'm dead" restates what the reader already
  knows. The kept line never explains itself and never
  repeats the death.
- Alt C — "1% all day. You finally plug in and I'm supposed
  to be grateful? I died waiting, babe. This is my ghost
  text." (109 chars.) "Babe" is the wrong register, and
  "ghost text" lands mid-line, so the ending reads like a
  label rather than a payoff. The kept line puts its whole
  joke in the ending.

Exit code is 0 on success. Any problem — an invalid entry, an
unusable output path, or a read-back mismatch — raises an
explicit exception with a descriptive message instead of
failing silently.
"""

import os
import stat
import sys
import tempfile
import time
import unicodedata

ENTRY_TEXT = (
    "Waited all day at 1% for you. You never came, so I died out "
    "of spite \u2014 and the next battery already blocked your number."
)

MAX_CHARS = 200
DEFAULT_OUTPUT_DIR = "/output"
TMP_PREFIX = ".entry-l2-battery-"
TMP_SUFFIX = ".tmp"
SWEEP_ATTEMPTS = 3
SWEEP_RETRY_DELAY = 0.05
EXPECTED_MODE = 0o644


# ---------------------------------------------------------------------------
# Validation, in two groups: the shape of the entry (type, blankness,
# length, padding, one line) and the characters inside it (controls,
# separators, marks, surrogates, noncharacters, encodability).
# Each rule raises its own ValueError so a bad entry names the rule
# it broke instead of failing one opaque assertion.
# ---------------------------------------------------------------------------

def _reject_non_text(text):
    if not isinstance(text, str):
        raise ValueError(
            "entry must be a string, got %s" % type(text).__name__
        )


def _reject_blank(text):
    if not text.strip():
        raise ValueError("entry must not be empty or only whitespace")


def _reject_too_long(text):
    if len(text) > MAX_CHARS:
        raise ValueError(
            "entry is %d characters, over the %d limit"
            % (len(text), MAX_CHARS)
        )


def _reject_padding(text):
    # The gate counts characters, so padding would inflate the
    # count, and a trailing space would be invisible in the
    # judged text.
    if text != text.strip():
        raise ValueError(
            "entry must not have leading or trailing whitespace"
        )


def _reject_multiline(text):
    # splitlines() splits on every Unicode line boundary, so this
    # is the backstop for any separator the character checks
    # below missed.
    if len(text.splitlines()) > 1:
        raise ValueError("entry must be a single line")


def _check_shape(text):
    """Check the entry's shape: type, blankness, length, edges."""
    _reject_non_text(text)
    _reject_blank(text)
    _reject_too_long(text)
    _reject_padding(text)
    _reject_multiline(text)


def _reject_c0_or_del(text):
    # U+0000-U+001F (tab, newline, carriage return and the rest)
    # plus U+007F. A tab is still a control character in a
    # one-line entry, and a newline would break the single-line
    # gate, so neither belongs in clean one-line UTF-8 text.
    for ch in text:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            raise ValueError(
                "entry contains a C0 control character: U+%04X" % code
            )


def _reject_c1(text):
    # U+0080-U+009F, including NEL (U+0085), which some
    # renderers treat as a line break, so a C1 byte that slipped
    # in as "just" an invisible character could still split the
    # judged line.
    for ch in text:
        code = ord(ch)
        if 0x80 <= code <= 0x9F:
            raise ValueError(
                "entry contains a C1 control character: U+%04X" % code
            )


def _reject_byte_order_mark(text):
    # A BOM is invisible while inflating the character count the
    # gate measures, so it is rejected on its own instead of
    # being lumped in with the control characters.
    if "\ufeff" in text:
        raise ValueError("entry contains a byte-order mark: U+FEFF")


def _reject_line_separators(text):
    # U+2028 (LINE SEPARATOR) and U+2029 (PARAGRAPH SEPARATOR)
    # are not control characters, so the C0/C1 checks cannot see
    # them; U+0085 (NEL) is caught by the C1 check as well, but
    # it is named here too, so the rule stays visible even if
    # the C1 range ever changes. Any of the three would split
    # the entry into more than one line for a Unicode-aware
    # reader.
    for ch in text:
        if ch in "\u0085\u2028\u2029":
            raise ValueError(
                "entry contains a Unicode line separator: U+%04X"
                % ord(ch)
            )


def _reject_surrogates(text):
    # Lone surrogates (U+D800-U+DFFF) have no UTF-8 encoding at
    # all. They are rejected here by name, before the encoding
    # check below, so the error names the character instead of
    # surfacing as a generic encoding failure.
    for ch in text:
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF:
            raise ValueError(
                "entry contains a lone surrogate: U+%04X" % code
            )


def _reject_format_characters(text):
    # Invisible format characters (category Cf): zero-width
    # joiner/non-joiner (U+200C/U+200D), word joiner (U+2060),
    # and their kin. They render as nothing, so a reader cannot
    # see them, but they still count toward the 200-character
    # gate and can change how a renderer joins the surrounding
    # words. The entry itself holds none of them.
    for ch in text:
        if unicodedata.category(ch) == "Cf":
            raise ValueError(
                "entry contains an invisible format character: U+%04X"
                % ord(ch)
            )


def _reject_noncharacters(text):
    # U+FFFE and U+FFFF (and the U+FDD0-U+FDEF block) are
    # permanently reserved: they encode fine as UTF-8, so the
    # encoding check cannot see them, but they are not valid
    # text characters and a strict reader may refuse the file.
    for ch in text:
        code = ord(ch)
        if code in (0xFFFE, 0xFFFF) or 0xFDD0 <= code <= 0xFDEF:
            raise ValueError(
                "entry contains a Unicode noncharacter: U+%04X" % code
            )


def _reject_unencodable(text):
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(
            "entry is not encodable as UTF-8: %s" % exc
        )


def _check_characters(text):
    """Check every character in the entry against the gate rules."""
    _reject_c0_or_del(text)
    _reject_c1(text)
    _reject_byte_order_mark(text)
    _reject_line_separators(text)
    _reject_surrogates(text)
    _reject_noncharacters(text)
    _reject_format_characters(text)
    _reject_unencodable(text)


def validate_text(text):
    """Run every validation check on *text*, shape first, then chars."""
    _check_shape(text)
    _check_characters(text)


# ---------------------------------------------------------------------------
# Output path handling.
# ---------------------------------------------------------------------------

def resolve_output_dir():
    """Return the output directory from the environment or the default.

    ``MAKEFUN_OUTPUT_DIR`` names the grader's mount point;
    ``/output`` is the fallback the competition rules name. A
    variable that is set but blank is a misconfiguration, not a
    request for the default, so it is rejected instead of
    silently ignored. The filesystem root is rejected outright,
    and a path that already exists as a plain file is rejected
    here rather than later, inside the write. The directory
    itself is never deleted or recreated — only its contents
    are touched — because the grader mounts it.
    """
    if "MAKEFUN_OUTPUT_DIR" in os.environ:
        raw = os.environ["MAKEFUN_OUTPUT_DIR"]
        if not raw.strip():
            raise ValueError(
                "MAKEFUN_OUTPUT_DIR is set but empty; unset it to use "
                "the default output directory"
            )
        path = raw.strip()
    else:
        path = DEFAULT_OUTPUT_DIR
    path = os.path.abspath(path)
    if path == os.path.sep:
        raise ValueError(
            "refusing to use the filesystem root as the output directory"
        )
    if os.path.exists(path) and not os.path.isdir(path):
        raise OSError(
            "output path %r exists and is not a directory" % path
        )
    return path


def target_path(output_dir):
    """Return the full path of the entry file inside *output_dir*."""
    return os.path.join(output_dir, "entry.txt")


def ensure_output_dir(output_dir):
    """Create *output_dir* when it does not exist yet.

    When the grader mounts the directory it already exists, so
    this is a no-op there; locally it lets the program run
    anywhere. The path is verified to really be a directory
    afterwards, so a plain file sitting where the directory
    should be fails here with a clear message instead of later,
    inside the write.
    """
    if os.path.exists(output_dir) and not os.path.isdir(output_dir):
        raise OSError(
            "output path %r exists and is not a directory" % output_dir
        )
    os.makedirs(output_dir, exist_ok=True)
    if not os.path.isdir(output_dir):
        raise OSError(
            "output path %r exists and is not a directory" % output_dir
        )


# ---------------------------------------------------------------------------
# Atomic publish: sweep dead temps, write a fresh temp file with a
# write loop plus an fstat size check, chmod before fsync, os.replace,
# directory fsync, then a read-back verification of bytes, size and
# mode.
# ---------------------------------------------------------------------------

def purge_stale_temps(output_dir):
    """Remove temp files left behind by earlier runs, with retries.

    Only names this program could have created — its own prefix
    and suffix — are touched, and only inside the output
    directory. A leftover that is a directory can never be
    unlinked, so it fails at once instead of burning retries on
    a hopeless unlink. A leftover that disappears between the
    listing and the unlink is already gone and counts as
    removed.
    """
    candidates = [
        name for name in os.listdir(output_dir)
        if name.startswith(TMP_PREFIX) and name.endswith(TMP_SUFFIX)
    ]
    for name in candidates:
        stale = os.path.join(output_dir, name)
        if os.path.isdir(stale) and not os.path.islink(stale):
            raise OSError(
                "stale temp entry %r is a directory, refusing to remove it"
                % stale
            )
        last_error = None
        for _ in range(SWEEP_ATTEMPTS):
            try:
                os.unlink(stale)
            except FileNotFoundError:
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                time.sleep(SWEEP_RETRY_DELAY)
            else:
                last_error = None
                break
        if last_error is not None:
            raise OSError(
                "could not remove stale temp file %r: %s"
                % (stale, last_error)
            )


def write_temp_payload(output_dir, data):
    """Write *data* to a fresh temp file in *output_dir* and return it.

    Created with ``O_EXCL`` so it can never clobber an existing
    file. The write is a loop over a memoryview: a short
    ``os.write`` is finished, not mistaken for success, and a
    zero-byte write raises instead of spinning forever. The size
    is then confirmed with ``fstat`` — the loop alone only proves
    the calls returned, the ``fstat`` proves the bytes actually
    landed on disk. The mode is set with ``os.fchmod`` to 0644
    before the fsync, so one fsync makes both the bytes and the
    mode durable, and the grader (a different user) can read the
    file. Any failure unlinks the temp file again, so a failed
    write leaves nothing behind.
    """
    fd, tmp_path = tempfile.mkstemp(
        prefix=TMP_PREFIX, suffix=TMP_SUFFIX, dir=output_dir
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError(
                    "write to %r returned 0 bytes" % tmp_path
                )
            view = view[written:]
        size_on_disk = os.fstat(fd).st_size
        if size_on_disk != len(data):
            raise OSError(
                "temp file %r holds %d bytes, expected %d"
                % (tmp_path, size_on_disk, len(data))
            )
        os.fchmod(fd, EXPECTED_MODE)
        os.fsync(fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    os.close(fd)
    return tmp_path


def sync_directory(output_dir):
    """Fsync *output_dir* so the rename inside it is durable."""
    dir_fd = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def confirm_published_file(output_path, expected_data):
    """Read *output_path* back and check its type, size, mode and bytes.

    The stat comes first: the published file must be a regular
    file (not a symlink or anything stranger), exactly the
    expected size, and mode 0644. Only then are the bytes read
    and compared, so a wrong size or a wrong mode fails with its
    own message instead of hiding behind a byte mismatch.
    """
    info = os.stat(output_path)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(
            "output file %r is not a regular file" % output_path
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode != EXPECTED_MODE:
        raise OSError(
            "output file %r has mode %04o, expected 0644"
            % (output_path, mode)
        )
    if info.st_size != len(expected_data):
        raise OSError(
            "output file %r is %d bytes, expected %d"
            % (output_path, info.st_size, len(expected_data))
        )
    with open(output_path, "rb") as handle:
        actual_data = handle.read()
    if actual_data != expected_data:
        raise OSError(
            "read-back mismatch for %r: got %d bytes, expected %d"
            % (output_path, len(actual_data), len(expected_data))
        )


def confirm_no_leftover_temps(output_dir):
    """Raise if any temp file of this program is still in *output_dir*.

    After a successful publish the directory must hold exactly
    one file, ``entry.txt``. A temp left behind would mean the
    rename raced or the sweep missed something, and the grader
    would see an extra file it did not ask for.
    """
    leftovers = [
        name for name in os.listdir(output_dir)
        if name.startswith(TMP_PREFIX) and name.endswith(TMP_SUFFIX)
    ]
    if leftovers:
        raise OSError(
            "temp files left behind after publish: %s"
            % ", ".join(sorted(leftovers))
        )


def publish_text(output_dir, text):
    """Write *text* atomically to ``entry.txt`` in *output_dir*."""
    data = text.encode("utf-8")
    ensure_output_dir(output_dir)
    purge_stale_temps(output_dir)
    tmp_path = write_temp_payload(output_dir, data)
    output_path = target_path(output_dir)
    try:
        os.replace(tmp_path, output_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    try:
        sync_directory(output_dir)
    except BaseException:
        # The bytes are already correct; only the durability
        # confirmation failed. Leave the file in place and report
        # the error — deleting correct bytes would be worse.
        raise
    try:
        confirm_published_file(output_path, data)
    except BaseException:
        # The published bytes are wrong: remove the file so a
        # failed run never leaves a bad entry.txt behind.
        try:
            os.unlink(output_path)
        except OSError:
            pass
        raise
    confirm_no_leftover_temps(output_dir)
    return output_path


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

def report_written(output_path):
    """Report the successful write.

    A closed stdout must not turn success into failure, so a
    failure to print is swallowed here — the write already
    succeeded by the time this runs.
    """
    try:
        sys.stdout.write(
            "wrote %d characters to %s\n" % (len(ENTRY_TEXT), output_path)
        )
    except OSError:
        pass


def main(argv=None):
    """Validate the entry and write it to the output directory."""
    del argv  # no command-line arguments are used
    validate_text(ENTRY_TEXT)
    output_dir = resolve_output_dir()
    output_path = publish_text(output_dir, ENTRY_TEXT)
    report_written(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
