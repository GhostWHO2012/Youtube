@echo off
chcp 65001 >nul
cd /d "%~dp0"

set REPO_URL=https://github.com/xcsh2000/Youtube.git
set GIT_DIR_PATH=%~dp0.gitdata
set WORK_TREE_PATH=%~dp0

echo.
echo 正在准备上传到 GitHub:
echo %REPO_URL%
echo.

git --git-dir="%GIT_DIR_PATH%" --work-tree="%WORK_TREE_PATH%" remote set-url origin %REPO_URL% 2>nul
if errorlevel 1 git --git-dir="%GIT_DIR_PATH%" --work-tree="%WORK_TREE_PATH%" remote add origin %REPO_URL%

git --git-dir="%GIT_DIR_PATH%" --work-tree="%WORK_TREE_PATH%" config http.sslverify true
git --git-dir="%GIT_DIR_PATH%" --work-tree="%WORK_TREE_PATH%" config http.sslbackend openssl

echo.
echo 如果这是第一次上传，会打开 GitHub 设备码登录流程。
echo 请按窗口提示打开网页、输入代码，并授权 xcsh2000 账号。
echo.

git -c http.sslVerify=true -c http.sslBackend=openssl credential-manager github login --username xcsh2000 --device

echo.
echo 正在推送 main 分支...
git -c http.sslVerify=true -c http.sslBackend=openssl --git-dir="%GIT_DIR_PATH%" --work-tree="%WORK_TREE_PATH%" push -u origin main

echo.
if errorlevel 1 (
  echo 上传失败。请把上面的错误信息发给 Codex。
) else (
  echo 上传完成：https://github.com/xcsh2000/Youtube
)
echo.
pause
