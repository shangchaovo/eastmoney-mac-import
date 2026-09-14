# eastmoney-mac-import

Mac 版东方财富**没有官方的自选股文件导入**。顶部菜单「导入工具」只接受 `.zip`，而且那不是股票列表。

这个脚本给已经安装并登录了 **Mac 东方财富客户端** 的用户，把一份代码列表写入自己的云端自选分组。写完后重启客户端即可同步。

不依赖某台电脑的用户名、不读取别人的 dashboard、也不需要把东财密码或 Cookie 填到任何网站。

## 要求

- macOS
- 已安装并**登录**东方财富 Mac 客户端
- Python 3.9+（系统自带即可，无第三方依赖）
- 给 **终端**（或你用来运行脚本的 App）打开「完全磁盘访问权限」，否则读不到沙盒里的登录文件

系统设置 → 隐私与安全性 → 完全磁盘访问权限。

## 代码文件格式

纯文本，每行一个 6 位 A 股代码：

```text
# comments are ok
600519
000001
300750
```

也可以 `代码,名称`：

```text
600519,贵州茅台
000001,平安银行
```

`sh600519` / `sz000001` 这种前缀会自动去掉。

## 用法

先预览，不写入：

```bash
python3 import_watchlist.py --file stocks.txt
```

确认无误后再写入云端：

```bash
python3 import_watchlist.py --file stocks.txt --apply
```

写完后重启东方财富 App。也可以让脚本帮忙重启：

```bash
python3 import_watchlist.py --file stocks.txt --apply --relaunch
```

自定义分组名：

```bash
python3 import_watchlist.py --file stocks.txt --apply --group-name 我的观察
```

双击 `Import.command`，或把 `stocks.txt` 拖到 `Import.command` 上（仍会先 dry-run；真正写入请用上面的 `--apply`）。

## 它做了什么

1. 从当前用户的标准路径读取东方财富登录态（`$HOME/Library/Containers/com.eastmoney.explore/...`）
2. 调用东方财富客户端使用的云端自选接口，新建或覆盖一个分组
3. 不会改你的默认「自选股」分组里的原有股票

## 安全

- 不要把 `ut` / `ct` 发到网上，也不要提交 `.env`
- 可选环境变量（一般不需要）：`EASTMONEY_UID` `EASTMONEY_UT` `EASTMONEY_CT` `EASTMONEY_CLIENT_ID`
- 本仓库不包含任何账号、Cookie 或本机路径

## 限制

- 仅 Mac 客户端。Windows「功能 → 自选导入」是另一套官方菜单，本脚本不覆盖
- 这是非官方接口，东方财富改版后可能失效
- 与东方财富官方无关联，使用风险自负
- 不是投资建议

## 自检

```bash
python3 import_watchlist.py --self-test
```
