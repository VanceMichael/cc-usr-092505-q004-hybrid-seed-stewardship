# 杂交稻制种受托与质量追溯

管理杂交稻制种合同、地块隔离、批次质量和匿名价格指数。

## 领域资料

`contracts/domain.schema.json` 记录共享资料结构，`fixtures/domain.json` 是不含真实个人信息的示例。参与方包括种业企业、制种受托人、县级管理人员、种子检验人员。

当前资料依据以下业务事实维护：

- 三明拥有多个国家级制种大县和大量种业企业
- 联盟发布新品种并建立受托人管理和分级认定制度
- 当地发布水稻制种价格指数并强调多性状协同育种

## 开发命令

- 运行测试：`python3 -m unittest discover -s tests -v`
- 编译或构建：`python3 -m compileall -q src/hybrid_seed_stewardship tests`

上述命令只读取仓库内文件，不连接外部业务服务。
