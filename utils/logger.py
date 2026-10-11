"""Logger for migration process and unsupported features."""

import sys
from datetime import datetime
from pathlib import Path
from typing import TextIO


class _Tee:
    """Mirror a stream to the terminal and the log file.

    Captures everything the migration prints, including bare print() calls,
    prompts and tracebacks, so the .log file matches the terminal exactly.
    """

    def __init__(self, stream: TextIO, log_file: TextIO):
        self._stream = stream
        self._log_file = log_file

    def write(self, data: str) -> int:
        written = self._stream.write(data)
        try:
            self._log_file.write(data)
            self._log_file.flush()
        except Exception:
            # Never let logging break the migration
            pass
        return written

    def flush(self) -> None:
        self._stream.flush()
        try:
            self._log_file.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        return self._stream.isatty()

    def __getattr__(self, name):
        return getattr(self._stream, name)


class MigrationLogger:
    """Logger for migration process and unsupported features."""

    def __init__(self, filename: str | None = None):
        # Get the migration directory and logs folder
        migration_dir = Path(__file__).parent.parent
        logs_dir = migration_dir / "logs"

        # Create logs directory if it doesn't exist
        logs_dir.mkdir(exist_ok=True)

        # Use timestamped filename if not specified
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"migration_{timestamp}.log"

        self.filename = logs_dir / filename
        self.unsupported_features: list[str] = []
        self.log_entries: list[str] = []

        # Open the log file up front and write every line as it happens, so a
        # crash or Ctrl+C still leaves a complete log on disk.
        self._file = open(self.filename, "w", encoding="utf-8", buffering=1)
        self._write_header()

        # Mirror stdout/stderr into the same file
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        sys.stdout = _Tee(self._stdout, self._file)
        sys.stderr = _Tee(self._stderr, self._file)

    def _write_header(self):
        self._file.write("=" * 80 + "\n")
        self._file.write("Discord to Fluxer Migration Log\n")
        self._file.write(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        self._file.write("=" * 80 + "\n\n")

    def log(self, message: str, level: str = "INFO"):
        """Log a message to console and file."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}] [{level}] {message}"
        self.log_entries.append(entry)
        # Written to the file via the stdout tee, so console and file stay in sync
        print(f"  {message}")

    def log_unsupported(self, feature: str, details: str = ""):
        """Log an unsupported feature."""
        message = f"Unsupported: {feature}"
        if details:
            message += f" - {details}"
        self.unsupported_features.append(message)
        self.log(message, "WARN")

    def save(self):
        """Finalise the log file and restore the original streams."""
        if self._file.closed:
            return

        # Restore streams first so the closing notice is not written twice
        sys.stdout = self._stdout
        sys.stderr = self._stderr

        if self.unsupported_features:
            self._file.write("\n" + "=" * 80 + "\n")
            self._file.write("UNSUPPORTED FEATURES\n")
            self._file.write("-" * 80 + "\n")
            for feature in self.unsupported_features:
                self._file.write(f"  • {feature}\n")

        self._file.write("\n" + "=" * 80 + "\n")
        self._file.write(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        self._file.write("=" * 80 + "\n")
        self._file.close()

        print(f"\n✓ Migration log saved to: {self.filename}")
