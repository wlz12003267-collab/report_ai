"""数据库连接工厂。"""
from typing import Optional

import pymysql
import psycopg
from iotdb.Session import Session

from configs.settings import get_settings


def get_db_connection_mysql():
    """建立 MySQL 连接。"""
    s = get_settings()
    try:
        conn = pymysql.connect(
            host=s.mysql_host,
            port=s.mysql_port,
            user=s.mysql_user,
            password=s.mysql_password,
            database=s.mysql_database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )
        print("✔ successful connection for mysql!")
        return conn
    except pymysql.MySQLError as e:
        print(f"Error connecting to MySQL: {e}")
        return None


def get_db_connection_postgres():
    """建立 PostgreSQL 连接。"""
    s = get_settings()
    try:
        conn = psycopg.connect(
            host=s.pg_host,
            port=s.pg_port,
            user=s.pg_user,
            password=s.pg_password,
            dbname=s.pg_database,
        )
        print("✔ successful connection for PostgreSQL!")
        return conn
    except Exception as e:
        print(f"Error connecting to PostgreSQL: {e}")
        return None


def get_db_connection_iot() -> Optional[Session]:
    """建立 IoTDB 连接。"""
    s = get_settings()
    try:
        session = Session(
            host=s.iotdb_host,
            port=s.iotdb_port,
            user=s.iotdb_user,
            password=s.iotdb_password,
            database=s.iotdb_database,
            fetch_size=1024,
        )
        session.open(False)
        print("✔ successful connection for Apache IoTDB!")
        return session
    except Exception as e:
        print(f"Error connecting to IoTDB: {e}")
        return None