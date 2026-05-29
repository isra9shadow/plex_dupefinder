#!/bin/bash
# OBSOLETE — This script pre-dates the two-pass quarantine design and is no longer useful.
# The current removal path is quarantine_files() + remove_plex_metadata() in plex_dupefinder.py.
# Files are moved to QUARANTINE_DIR (not deleted with rm), and a .dupefinder_meta.json sidecar
# is written alongside each moved file with a ready-to-run restore_command field.
# To restore a quarantined file, copy the restore_command from the sidecar JSON and run it in a shell.
#
# Original comment (retained for history):
# Since plex is having such great troubles deleting files I wrote this little bash script to read the decisions.log and delete the files after it's been run.

inputfile=$1
while read -r line
do
          if [[ "$line" == *"Removing : {"* ]]; then
             var=$(echo "${line}" | grep Removing | sed 's/^.*\(file.*multipart\).*$/\1/' | sed -r 's/^.{9}//' | sed 's/.\{14\}$//')
             rm "${var}"
          fi
done < "$inputfile"
