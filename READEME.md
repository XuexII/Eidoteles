# 1. 快速开始

```bash
conda create -n sql-agent python mysql-server mysql -c conda-forge

conda activate sql-agent

# 替换为自己的项目路径
cd /Users/dony/workspace/localProjects/Eidoteles

pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
# 将--datadir替换为自己的路径
mysqld --initialize-insecure --user=root --datadir=/Users/dony/workspace/datasets/sql

mysqld --user=root --datadir=/Users/dony/workspace/datasets/sql --port=3306

# 数据导入mysql
python db.py

# 启动服务
python main.py
```