#!/bin/bash
set -e
service ssh start
exec python -m src
