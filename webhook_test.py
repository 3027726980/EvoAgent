# 作者：LHF
# 时间：2026-11-02
# 作用：验证 EvoAgent 的 GitHub PR Webhook 接收和自动审查链路。


def webhook_test():
    """输出测试标记，供 EvoAgent 检查新增的 print 调用。"""
    # 保留文档中的 print 测试样例，供 PR 自动审查识别。
    print("EvoAgent webhook test")
