人工智能东盟出海组合准备度与风险治理台

共同政策证据：Repository.register_policy 按事件身份保存同一政策证据及修订号，
link_policy_project 将多个项目关联同一事件；open_policy_review 固定项目证据版本、
政策修订和暴露，resolve_policy_review 拒绝在结论前被改过的证据；
policy_impact 可查询跨市场共享的项目和旧尽调。已决旧记录不因政策补报而改写。

运行验收：
python3 -m export_portfolio run --fixture fixtures/acceptance.json --database /tmp/export_portfolio.sqlite3 --output /tmp/export_portfolio-output

核验：
python3 -m export_portfolio verify --database /tmp/export_portfolio.sqlite3 --output /tmp/export_portfolio-output

全量测试：
python3 -m unittest discover -s tests -v

按模块测试：
python3 -m unittest tests.intake.test_initiative -v

构建检查：
python3 -m compileall -q export_portfolio tests

resource_book.capacity 登记共享伙伴或认证资源及承诺版本，require 登记项目需求，approve 在事务中合计占用并核对承诺；已批准项目需求和批准记录不可改写，inspect 可追溯当前剩余能力。

董事会组合评议：review_board.register_forecast 按项目登记情景预测及修订号，预测必须给出多情景概率区间，单一数字、零区间或确定概率不能包装成保证收益；recommend 在审议前枚举候选组合，说明收益区间、共享资源占用与放弃其他组合的代价；open_review 固定项目依据、预测修订与伙伴承诺修订，approve_review 在同一事务中重核项目依据与当前剩余能力，伙伴承诺变更或两个评议同时争取同一资源都不会造成超额批准；批准只推进选中的项目，保留实际占用与当时采用的预测，后来的预测只能发起新评议，不能改写旧决定。
