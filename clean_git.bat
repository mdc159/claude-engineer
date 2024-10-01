@echo off
git rm -r --cached mven/
git rm -r --cached env/
git rm -r --cached code_execution_env/
git commit -m "Remove virtual environment files from version control"
git push
