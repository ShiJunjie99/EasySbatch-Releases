Python >= 3.11; standard library only.

# Single-user deployment smoke

Run from this project directory:

```bash
python3 hello.py
```

The program prints its host, working directory, Linux username, Python version
and Slurm job ID, waits three seconds, and exits. No input files or third-party
packages are required. It does not launch other programs.

For the deployment filesystem check, the operator may additionally pass
`--run-directory` with the exact run directory shown in Final Review. The program
then reads that directory's `submit.sh` and reports readability, without printing
the file contents or any environment-variable values other than `SLURM_JOB_ID`.
