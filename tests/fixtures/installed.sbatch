#!/usr/bin/env bash

#SBATCH --job-name=installed-demo
#SBATCH --partition=example_cpu
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=4096M
#SBATCH --time=00:30:00
#SBATCH --chdir=/shared/gromacs-demo

set -e

# Environment
'source' /etc/profile.d/modules.sh
'module' load gromacs/2026.0

set -e
set -u
set -o pipefail

# Working directory
cd -- /shared/gromacs-demo

# Required inputs (relative to work_dir)
test -r mdp/em.mdp
test -r polymer/resin_bcb_melt.gro
test -r topol.top
test -r forcefield/martini_v3_subset.itp
test -r topology/resin_bcb.itp

# Prepare step 1
(
'gmx_mpi' grompp -f mdp/em.mdp -c polymer/resin_bcb_melt.gro -p topol.top -o em.tpr
)

# Run
'gmx_mpi' mdrun -deffnm em -ntomp 1
