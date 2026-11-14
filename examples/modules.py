"""本地示例：两个模块串联抛出嵌套错误。

模块B（order_store，订单存储层）抛出底层连接错误，错误对象里带有
环境变量字段与堆栈；模块A（payment_service，支付服务）捕获后把它
包装进自己业务错误的 ``cause`` 字段，形成两模块嵌套错误链。
"""


def module_b_fetch_order():
    """模块B：订单存储层，返回底层结构化错误对象。"""
    return {
        "code": "DB_CONN_TIMEOUT",
        "message": "order store connection timeout after 3000ms",
        "env": {
            "DB_HOST": "10.0.0.8",
            "DB_PASSWORD": "s3cret-db-pass",
            "APP_ENV": "prod",
        },
        "secret_fields": ["env.DB_PASSWORD"],
        "stack": [
            "order_store.connect:81",
            "order_store._open_socket:44",
        ],
    }


def module_a_charge():
    """模块A：支付服务，把模块B的错误包装为嵌套原因。"""
    cause = module_b_fetch_order()
    return {
        "code": "PAYMENT_DECLINED",
        "message": "charge failed for order #A-1001",
        "cause": cause,
        "env": {
            "GATEWAY_API_TOKEN": "tok-live-9f3c",
            "APP_ENV": "prod",
        },
        "secret_fields": ["env.GATEWAY_API_TOKEN"],
        "stack": [
            "payment_service.charge:120",
            "payment_service._authorize:58",
        ],
    }
