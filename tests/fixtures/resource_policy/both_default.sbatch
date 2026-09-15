#!/usr/bin/env bash

#SBATCH --job-name=python-demo
#SBATCH --partition=example_cpu
#SBATCH --account=research
#SBATCH --qos=normal
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --chdir="/shared/python demo"
#SBATCH --output="/shared/python demo/logs/slurm-%j.out"
#SBATCH --error="/shared/python demo/logs/slurm-%j.err"

set -e

# Environment
'source' /opt/example/conda/etc/profile.d/conda.sh
'conda' activate /opt/example/envs/python

set -e
set -u
set -o pipefail

# Working directory
cd -- '/shared/python demo'

# Required inputs (relative to work_dir)
test -r run.py
test -r 'inputs/case 01.json'

# Run
'python' run.py --input 'inputs/case 01.json'
