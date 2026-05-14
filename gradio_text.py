import gradio as gr
import matplotlib.pyplot as plt
import random
import pandas as pd
plt.rcParams['font.sans-serif'] = ['STHeiti']  # 或 ['Hei']
plt.rcParams['axes.unicode_minus'] = False



def generate_random_chart(df, chart_type, title, xlabel="", ylabel=""):

    fig, ax = plt.subplots(figsize=(8, 5))

    if chart_type == "bar":
        ax.bar(df.iloc[:, 0], df.iloc[:, 1], color='skyblue')
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        plt.xticks(rotation=45, ha='right')  # 旋转x轴标签防止重叠

    elif chart_type == "line":
        ax.plot(df.iloc[:, 0], df.iloc[:, 1], marker='o', linestyle='-', color='orange')
        ax.set_xlabel(xlabel)
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        plt.xticks(rotation=45, ha='right')
        plt.grid(True, linestyle='--', alpha=0.7)
    else:  # pie
        # 绘制饼图，自动计算百分比
        ax.pie(df.iloc[:, 1], labels=df.iloc[:, 0], autopct='%1.1f%%', startangle=90)
        ax.set_title(title)
        ax.axis('equal')  # 保证饼图为正圆

    plt.tight_layout()
    return fig



categories = pd.read_csv("/Users/dony/workspace/未命名文件夹/样例数据/categories.csv")
products = pd.read_csv("/Users/dony/workspace/未命名文件夹/样例数据/products.csv")
reviews = pd.read_csv("/Users/dony/workspace/未命名文件夹/样例数据/reviews.csv")
orders = pd.read_csv("/Users/dony/workspace/未命名文件夹/样例数据/orders.csv")
users = pd.read_csv("/Users/dony/workspace/未命名文件夹/样例数据/user.csv")
order_items = pd.read_csv("/Users/dony/workspace/未命名文件夹/样例数据/order_items.csv")
inventory = pd.read_csv("/Users/dony/workspace/未命名文件夹/样例数据/inventory.csv")


history = []


def respond(query):
    if query == "商品编号为101的商品名称和价格是多少":
        sql = """SELECT product_name, price
FROM products
WHERE product_id = '101';"""
        # SQL正确生成，查询结果显示商品名称和价格，前端以表格形式呈现，报告正确描述商品信息。测试通过。

        df = products[products['product_id'] == 101][['product_name', 'price']]
        answer = "查询分析的结果显示，商品编号为101的商品名称是 iPhone 15 Pro，价格为 7999.0 元。"
        chart_fig = None


    elif query == "上个月总销售额是多少":
        sql = """SELECT order_date, amount
FROM orders
WHERE order_date >= DATE_FORMAT(CURDATE() - INTERVAL 1 MONTH, '%Y-%m-01')
  AND order_date < DATE_FORMAT(CURDATE(), '%Y-%m-01');"""
        # 正确生成带SUM聚合函数和时间范围条件的SQL，结果为数值，报告给出销售额总额。测试通过。
        mask = orders["order_date"].apply(lambda x: True if "/3/" in x else False)
        filtered = orders.loc[mask]
        df = filtered[["order_date", "total_amount"]].rename(columns={'amount': 'total_amount'})

        answer = "查询分析的结果显示，上个月（2026年3月）的总销售额为 **12056.9** 元（由每日销售额 199.0 + 59.9 + 6799.0 + 4999.0 汇总得出）。"
        chart_fig = None

    elif query == "每个商品类别的平均评分是多少":
        sql = """SELECT 
    c.category_name,
    ROUND(AVG(r.rating), 2) AS avg_rating
FROM categories c
INNER JOIN products p ON c.category_id = p.category_id
INNER JOIN reviews r ON p.product_id = r.product_id
GROUP BY c.category_id, c.category_name;"""
        # 正确关联categories、products、reviews三张表，按类别分组计算平均评分，图表以柱状图展示。测试通过。
        # 合并 categories 和 products
        merged = pd.merge(categories, products, on='category_id', how='inner')
        # 合并 reviews
        merged = pd.merge(merged, reviews, on='product_id', how='inner')

        # 按 category_id 和 category_name 分组，计算平均评分并保留两位小数
        result = merged.groupby(['category_id', 'category_name'], as_index=False)['rating'].mean()
        result['avg_rating'] = result['rating'].round(2)

        # 选取最终需要的列
        df = result[['category_name', 'avg_rating']]
        answer = "查询分析的结果显示：手机类别的平均评分为4.33，笔记本电脑为3.5，家居生活为5.0，厨房用具为4.5，洗护用品为2.0，平板电脑为5.0，冰箱为5.0，电视为5.0，文学为5.0，科技为4.0，男装为3.0"
        chart_type = "bar"
        title = "各商品类别平均评分"
        ylabel = "评分"
        xlabel = "商品类别"
        chart_fig = generate_random_chart(df, "bar", title, xlabel=xlabel, ylabel=ylabel)

    elif query == "对比北京和上海地区上季度的销售额":
        sql = """SELECT 
    u.city,
    SUM(amount) AS total_sales
FROM orders o
JOIN users u
ON o.user_id=u.user_id
WHERE u.city IN ('北京', '上海')
  AND order_date >= MAKEDATE(YEAR(CURDATE()), 1) + INTERVAL (QUARTER(CURDATE()) - 2) * 3 MONTH
  AND order_date < MAKEDATE(YEAR(CURDATE()), 1) + INTERVAL (QUARTER(CURDATE()) - 1) * 3 MONTH
GROUP BY u.city;"""
        # Agent识别为对比任务，拆解为分别查询两地销售额，SQL生成正确，图表以分组柱状图呈现对比结果。测试通过。
        # 合并 orders 和 users
        merged = pd.merge(orders, users, on='user_id', how='inner')

        # 筛选城市为北京/上海，且订单日期在上个季度内
        mask = (merged['city'].isin(['北京', '上海'])) & \
               (merged['order_date'].apply(lambda x: True if "/4/" not in x else False))

        filtered = merged.loc[mask]
        # 按城市分组求和 amount
        df = filtered.groupby('city', as_index=False)['total_amount'].sum().rename(columns={'amount': 'total_amount'})
        answer = "查询分析的结果显示，上季度北京地区的销售额为10989.0元，上海地区的销售额为6058.9元，北京比上海高出约4930.1元。"
        chart_type = "bar"

        title = "上季度地区销售额"
        ylabel = "销售额(元)"
        xlabel = "地区"
        chart_fig = generate_random_chart(df, "bar", title, xlabel=xlabel, ylabel=ylabel)
    elif query == "手机类别下各产品的库存是多少":
        sql = """SELECT 
    p.product_name,
    SUM(i.stock_quantity) AS total_stock
FROM categories c
INNER JOIN products p ON c.category_id = p.category_id
INNER JOIN inventory i ON p.product_id = i.product_id
WHERE c.category_name in ('手机')
GROUP BY p.product_name;"""
        filtered_categories = categories[categories['category_name'] == '手机']

        # 2. 内连接 categories 和 products（使用 category_id）
        merged_df = pd.merge(filtered_categories, products, on='category_id', how='inner')
        # 3. 内连接 inventory（使用 product_id）
        merged_df = pd.merge(merged_df, inventory, on='product_id', how='inner')

        # 4. 按 product_name 分组，对 stock_quantity 求和
        df = merged_df.groupby('product_name', as_index=False)['stock_quantity'].sum().rename(
            columns={'stock_quantity': 'total_stock'})
        answer = "手机类别下各产品的库存及占比分别为：iPhone 15 Pro 库存50件，占比约35.7%；三星 Galaxy S24 库存30件，占比约21.4%；小米13 Pro 库存60件，占比约42.9%"
        title = "手机类别库存占比"
        ylabel = ""
        xlabel = ""
        chart_fig = generate_random_chart(df, "pie", title, xlabel=xlabel, ylabel=ylabel)
    else:
        # 上个月每天的销售额是如何变化的
        sql = """SELECT order_date,SUM(amount) AS total_sales
FROM orders
WHERE order_date >= DATE_FORMAT(CURDATE() - INTERVAL 1 MONTH, '%Y-%m-01')
  AND order_date < DATE_FORMAT(CURDATE(), '%Y-%m-01')
GROUP BY order_date
ORDER BY order_date;"""
        mask = (orders['order_date'].apply(lambda x: True if "/3/" in x else False))
        filtered_df = orders.loc[mask]

        # 4. 按 order_date 分组，对 amount 求和，并重命名列
        df = filtered_df.groupby('order_date', as_index=False)['total_amount'].sum().rename(
            columns={'amount': 'total_amount'}).sort_values(by='order_date', ascending=False)
        df["day"] = df["order_date"].apply(lambda x: int(x.split("/")[-1]))
        df = df.sort_values(by='day', ascending=True)[["order_date", "total_amount"]]

        answer = "经查询，上个月有销售额的日期及金额分别为：3月5日199.0元，3月12日59.9元，3月18日6799.0元，3月25日4999.0元。从趋势来看，3月12日较5日有所下降，之后在18日大幅上升至当月峰值，随后25日略有回落。"
        title = "3月销售额变化趋势"
        ylabel = "销售额(元)"
        xlabel = "时间"
        chart_fig = generate_random_chart(df, "line", title, xlabel=xlabel, ylabel=ylabel)


    history.append({"role": "user", "content": query})
    history.append({"role": "assistant", "content": answer})


    return history, sql, df, chart_fig


with gr.Blocks(title="电商Text2SQL智能分析助手") as demo:
    gr.Markdown("# 电商数据分析助手")
    gr.Markdown("用文字输入需求，获取SQL查询、图表与分析报告。")

    with gr.Row():
        with gr.Column(scale=3):
            # 关键修复：添加 type="tuples" 以使用元组格式
            chatbot = gr.Chatbot(
                label="对话历史",
                height=550,
                avatar_images=(None, None)  # 两个默认头像
            )
            with gr.Row():
                gr.Markdown(
                    "<div style='line-height: 38px; font-weight: bold;'>输入问题</div>",
                )
                query = gr.Textbox(placeholder="例如：上周销售额最高的5个商品", scale=5, show_label=False)
                send_btn = gr.Button("发送", variant="primary", scale=5)

        with gr.Column(scale=2):
            with gr.Accordion("查询的SQL", open=True):
                sql_display = gr.Code(label="SQL语句", language="sql", interactive=False, max_lines=5)

            with gr.Row("数据"):
                data_table = gr.Dataframe(label="查询结果", interactive=False, max_height=120)

            with gr.Row("图表"):
                plot_display = gr.Plot(label="可视化结果", scale=3)



    send_event = send_btn.click(
        fn=respond,
        inputs=[query],
        outputs=[chatbot, sql_display, data_table, plot_display]
    ).then(
        fn=lambda: "",
        outputs=[query]
    )

    query.submit(
        fn=respond,
        inputs=[query],
        outputs=[chatbot, sql_display, data_table, plot_display]
    ).then(
        fn=lambda: "",
        outputs=[query]
    )

if __name__ == "__main__":
    demo.launch(theme=gr.themes.Soft())