#!/bin/bash
# Post-merge setup: make sure Python deps match requirements.txt after a task merge.
set -e

pip install -r requirements.txt --quiet
