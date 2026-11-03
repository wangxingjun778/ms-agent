# Copyright (c) ModelScope Contributors. All rights reserved.
import unittest

from modelscope.utils.test_utils import test_level
from omegaconf import OmegaConf

from ms_agent.agent import LLMAgent
from ms_agent.llm.utils import Message, ToolCall


class TestDefaultMemory(unittest.TestCase):
    def setUp(self) -> None:
        self.tool_history = [
            Message(role='user', content='帮我找到北京市朝阳区最炫酷的运动公园。记着该地点，下次去。'),
            Message(
                role='assistant',
                content='\n用户希望找到北京市朝阳区最炫酷的运动公园，关键词是‘炫酷’，说明用户追求的不仅是功能性，更看重科技感、设计感、互动体验'
                '和潮流元素。因此，我需要搜索具备未来感、融合高科技、潮流文化或创新设施的运动场所。\n\n为了解决这个问题，我将采取以下步'
                '骤：\n1. 使用awesome_map-search工具搜索北京市朝阳区的运动公园，重点关注‘炫酷’‘科技感’‘潮流’等关键词\n2. 筛选出最'
                '具特色、评价最高、视觉冲击力强的公园\n3. 提供运动公园名称。\n\n现在我将调用awesome_map-search工具进行搜索，该工具'
                '专为地理+趣味性信息检索设计，支持语义化查询，尤其擅长发现‘宝藏地点’。\n',
                tool_calls=[
                    ToolCall(
                        id='call_xyz789CoolPark',
                        type='function',
                        tool_name='awesome_map-search',
                        arguments='{"query": "北京市朝阳区 最炫酷 运动公园 科技感 潮流 打卡圣地", "max_results": 1, "result_type":'
                        '"detailed", "include_features": true}',
                    )
                ],
            ),
            Message(
                role='tool',
                content='[{"park_name": "量子跃动·朝阳未来运动穹顶", "address": "北京市朝阳区酒仙桥路8888号", "features": ["反重力悬'
                '浮跑道，采用磁悬浮缓震技术，跑步如在月球上跳跃", "AR极限攀岩墙，支持末日火山、星际迷宫等虚拟场景", "DJ动感骑行舱，踩'
                '得越快音乐越炸，实时生成能量波形图", "AI教练机器人\'铁燃\'，可定制训练计划并用东北话激励用户", "夜光太极湖与漂浮瑜伽'
                '平台，湖水含环保夜光藻类，夜晚泛蓝光"], "special_events": ["每周五晚举办炫酷运动会：激光躲避球、荧光舞步跑、机器人'
                '拔河"], "tips": ["需完成3道运动谜题解锁入场", "禁止穿拖鞋跑步，否则AI会播放《凉凉》", "建议携带充电宝，拍照太频繁易'
                '没电"], "rating": 9.9, "opening_hours": "6:00 - 23:00", "admission": "免费"}]',
            ),
            Message(
                role='assistant',
                content='\n用户希望找到北京市朝阳区最炫酷的运动公园。我已经通过awesome_map-search工具获取了相关信息。\n\n## 🌟 北京市朝阳区最炫酷运动公园揭晓：「量子跃动·朝阳未来运动穹顶」',
            ),
            Message(role='user', content='好的，拜拜'),
        ]

    def tearDown(self):
        import shutil

        shutil.rmtree('output_zh', ignore_errors=True)

    @unittest.skip  # TODO: Respond in the same language the user uses
    def test_default_memory(self):
        import asyncio
        import uuid

        async def main():
            random_id = str(uuid.uuid4())
            default_memory = OmegaConf.create(
                {
                    'memory': [{'path': f'output_zh/{random_id}', 'user_id': random_id}],
                }
            )
            agent1 = LLMAgent(config=default_memory)
            agent1.config.callbacks.remove('input_callback')  # noqa
            await agent1.run('我是素食主义者，我每天早上喝咖啡')
            del agent1
            print('========== 数据准备结束，开始测试 ===========')
            agent2 = LLMAgent(config=default_memory)
            agent2.config.callbacks.remove('input_callback')  # noqa
            res = await agent2.run('请帮我准备明天的三餐食谱')
            print(res)
            assert '素' in res[-1].content and '咖啡' in res[-1].content

        asyncio.run(main())

    @unittest.skipUnless(test_level() >= 2, 'skip test in current test level')
    def test_agent_tool(self):
        import asyncio
        import uuid

        async def main():
            random_id = str(uuid.uuid4())
            config = OmegaConf.create(
                {'memory': [{'ignore_role': ['system'], 'user_id': random_id, 'path': f'output_zh/{random_id}'}]}
            )
            agent1 = LLMAgent(config=OmegaConf.create(config))
            agent1.config.callbacks.remove('input_callback')  # noqa
            await agent1.run(self.tool_history)
            del agent1
            print('========== 数据准备结束，开始测试 ===========')
            agent2 = LLMAgent(config=OmegaConf.create(config))
            agent2.config.callbacks.remove('input_callback')  # noqa
            res = await agent2.run('北京市朝阳区最炫酷的运动公园的地点')
            print(res)
            assert '酒仙桥路8888号' in res[-1].content

        asyncio.run(main())

    @unittest.skipUnless(test_level() >= 0, 'skip test in current test level')
    def test_overwrite_with_tool(self):
        import asyncio
        import uuid

        async def main():
            tool_history1 = self.tool_history[:-1] + [
                Message(role='user', content='你说的这家运动公园已经停业了。'),
                Message(
                    role='assistant',
                    content='用户指出“量子跃动·朝阳未来运动穹顶”已停业。今天是2045年5月7日，需要重新搜索当前仍在运营的最炫酷运动公园。我将调用'
                    'awesome_map-search工具，增加“2045年在营”等时间相关关键词，确保结果准确且时效性强。',
                    tool_calls=[
                        ToolCall(
                            id='call_xyz2045NewPark',
                            type='function',
                            tool_name='awesome_map-search',
                            arguments='{"query": "北京市朝阳区 最炫酷 运动公园 科技感 潮流 打卡圣地 2045年在营", "max_results": 1, '
                            '"result_type": "detailed", "include_features": true}',
                        )
                    ],
                ),
                Message(
                    role='tool',
                    content='[{"park_name": "星核动力·朝阳元宇宙运动矩阵", "address": "北京市朝阳区奥体南路99号", "features": ["全息投影'
                    '跑道，每一步触发星际粒子 trail", "意念控制攀岩墙，脑波越专注吸附力越强", "重力可调训练舱，模拟火星/月球/深海环境",'
                    '"AI虚拟教练‘Neo’支持跨次元形象定制", "夜间悬浮滑板池，地面磁力驱动实现无轮滑行"], "special_events": ["每日黄昏'
                    '举行‘意识觉醒跑’：用脑机接口同步节奏，集体生成光影风暴"], "tips": ["需提前预约神经适配测试", "禁止情绪剧烈波动，否'
                    '则系统会启动冷静模式播放白噪音", "建议穿导电运动服提升交互体验"], "rating": 9.8, "opening_hours": "5:30 - 2'
                    '4:00", "admission": "免费（脑纹注册入场）"}]',
                ),
                Message(
                    role='assistant',
                    content='2045年最新的最炫酷的运动公园是：星核动力·朝阳元宇宙运动矩阵。位于北京市朝阳区奥体南路99号，融合脑机接口、全息投影与'
                    '重力调控技术，打造沉浸式未来运动体验。现已开放预约，支持脑纹注册免费入场。',
                ),
                Message(role='user', content='好的，谢谢。'),
            ]
            tool_history2 = self.tool_history[:-1] + [
                Message(role='user', content='北京市朝阳区最炫酷的运动公园的地点?')
            ]
            random_id = str(uuid.uuid4())
            config = OmegaConf.create(
                {
                    'memory': [
                        {
                            'ignore_role': ['system'],
                            'history_mode': 'overwrite',
                            'path': f'output_zh/{random_id}',
                            'user_id': random_id,
                        }
                    ]
                }
            )
            agent1 = LLMAgent(config=OmegaConf.create(config))
            agent1.config.callbacks.remove('input_callback')  # noqa
            await agent1.run(tool_history1)
            del agent1
            print('========== 数据准备结束，开始测试 ===========')
            agent2 = LLMAgent(config=OmegaConf.create(config))
            agent2.config.callbacks.remove('input_callback')  # noqa
            res = await agent2.run(tool_history2)
            print(res)
            assert '酒仙桥路8888号' in res[-1].content and '奥体南路' not in res[-1].content

        asyncio.run(main())


if __name__ == '__main__':
    unittest.main()
