#!/usr/bin/env bash

#SBATCH --job-name=compiled-demo
#SBATCH --partition=example_cpu
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4096M
#SBATCH --time=1-01:01:01
#SBATCH --chdir=/shared/compiled-demo

set -e

# Environment
'source' /etc/profile.d/modules.sh
'module' load gcc/12 cmake/3.27

set -e
set -u
set -o pipefail

# Working directory
cd -- /shared/compiled-demo

# Required inputs (relative to work_dir)
test -r CMakeLists.txt
test -r inputs/case01.json

# Prepare step 1
(
'cmake' -S . -B build
)

# Prepare step 2
(
'cmake' --build build --parallel 1
)

# Compiled target
test -f ./build/solver
test -x ./build/solver

# Run
'./build/solver' --input inputs/case01.json
