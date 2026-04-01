# 添加 hosts 条目 - 需要管理员权限
Add-Content -Path "C:\Windows\System32\drivers\etc\hosts" -Value "`n127.0.0.1 api.openai.com # Trae-Proxy" -Force
Write-Host "hosts 文件已更新"

# 验证
$hostEntry = Get-Content "C:\Windows\System32\drivers\etc\hosts" | Select-String "api.openai.com"
Write-Host "验证结果：$hostEntry"
