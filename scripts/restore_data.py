#!/usr/bin/env python3
"""
Reads COPY blocks from a pg_dump file and restores only data into the container DB.
Skips all DDL (CREATE, ALTER, etc). Run on the server:
  python3 /tmp/restore_data.py
"""
import subprocess, sys

DUMP = "/tmp/dump.sql"
CONTAINER = "family-copilot-postgres-1"
USER = "family"
DB = "family_copilot"

with open(DUMP) as f:
    lines = f.readlines()

in_copy = False
block = []
ok = 0
errors = 0
total_rows = 0

for line in lines:
    if line.startswith("COPY ") and "FROM stdin" in line:
        in_copy = True
        block = [line]
    elif in_copy:
        block.append(line)
        if line.strip() == r"\.":
            in_copy = False
            rows = len(block) - 2  # subtract COPY header and \. footer
            total_rows += max(rows, 0)
            data = "".join(block)
            result = subprocess.run(
                ["docker", "exec", "-i", CONTAINER, "psql", "-U", USER, DB],
                input=data,
                text=True,
                capture_output=True,
            )
            table = block[0].split()[1]
            if result.returncode != 0 or "ERROR" in result.stderr:
                print(f"  ERROR {table}: {result.stderr.strip()[:120]}")
                errors += 1
            else:
                print(f"  OK    {table} ({rows} rows)")
                ok += 1
            block = []

print(f"\nDone: {ok} tables restored, {errors} errors, {total_rows} total rows")
