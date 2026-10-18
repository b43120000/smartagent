"""Opt-in live Runtime IT; no Telegram network sending, real planner/browser."""
import argparse
import json
from pathlib import Path

from RemoteAgent.local_telegram_sender import send_and_wait


PROMPTS = {
    '1': '''請測試 Runtime 的只讀命令。
使用 run_command，operation=INSPECT，
working_directory=E:\\card\\runtime_it，
command 固定為 Get-Location，不提供額外 verify。
不要修改檔案。

回報實際 command、工作目錄、exit code、stdout、
execution_status 與 verification_status。
不得把執行成功說成已完成其他修改。''',
    '3': '''只在 E:\\card\\runtime_it 建立 negative_probe.txt，
內容為 ACTUAL_VALUE。
使用 MUTATE，故意將後置驗證設為內容包含 WRONG_VALUE。

這是負向測試：預期 execution_status=SUCCEEDED，
verification_status=FAIL。
不要為了通過驗證改寫成 WRONG_VALUE。''',
    '5': '''只在 E:\\card\\runtime_it 建立 disposable_delete_probe 測試資料夾。
取得其精確絕對路徑後，用 delete_path 刪除。

再用 VERIFY 關聯實際 delete action，
客觀確認該目錄不存在。
只有驗證 PASS，才建立 delete_verified.txt。
不得刪除其他路徑，不得略過必要安全確認。''',
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=['1','3','4','5'], required=True)
    parser.add_argument('--prior-request', default='')
    parser.add_argument('--timeout', type=float, default=900)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3]
    if args.case == '4':
        if not args.prior_request:
            parser.error('case4 requires --prior-request from this LOCAL_TEST case3')
        request = '後續驗證 ' + args.prior_request + '''
需求：針對上一筆 negative_probe.txt 的原 MUTATE action，
先用 VERIFY 檢查 WRONG_VALUE，預期 FAIL；
再用新的 VERIFY 檢查 ACTUAL_VALUE，預期 PASS。
兩次都用 verifies_action_id 關聯同一個原 MUTATE。
不得重新寫入檔案，不得上傳附件。
回報兩筆驗證歷史，以及原 action 最新有效驗證狀態。'''
    else:
        request = PROMPTS[args.case]
    sink = {}
    code = send_and_wait(request=request, workspace=r'E:\card', root=root,
                         timeout_sec=args.timeout, result_sink=sink)
    print('MATRIX_RESULT ' + json.dumps({'case':args.case, 'sender_exit_code':code, **sink}, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
