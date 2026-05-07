import psycopg2
import sys

url = "postgresql://postgres.rxwqgfpubtvolorkuwpj:Timetable%21%40%23%24%21%40@aws-1-ap-northeast-2.pooler.supabase.com:6543/postgres"

try:
    conn = psycopg2.connect(url, connect_timeout=5)
    print("SUCCESS")
    conn.close()
    sys.exit(0)
except Exception as e:
    print(f"Failed: {e}")
