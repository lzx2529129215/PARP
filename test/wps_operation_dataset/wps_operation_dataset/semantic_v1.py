"""Manually reviewed, source-pinned semantic annotations. No remote dependencies.

A marks an action recorded, E only an entry point, I request only, C a goal/action
conflict. None of these asserts successful execution in Office or WPS.
"""

SOURCE_SHA256 = 'b68aeac87e7dd71be071ea2cce2921619783f0b00d1060876e85c913fdf642e3'

# code | family | name | merge parameters | exclusion / boundary
CATALOG = '''
UI_CUSTOMIZE|界面与视图|自定义功能区或快速访问工具栏|target,tab,command|不含文档内容编辑；功能区与快速工具栏保留不同 target
UI_THEME|界面与视图|查看或设置界面主题|mode,theme|不含幻灯片设计主题；当前只有请求或入口证据
DESIGN_MODE|界面与视图|切换控件设计模式|enabled|不含演示设计主题；切换按钮不能证明最终开关状态
VIEW_LAYOUT|界面与视图|切换文档视图与显示选项|view,show_whitespace|不含页面打印尺寸设置
VIEW_GUIDES|界面与视图|设置网格线、参考线与吸附|guide_type,enabled,snap|不合并为文档绘图；开关终态未核验
VIEW_PANE|界面与视图|打开导航或评论窗格|pane|打开评论窗格不等于新增或删除评论
PRINT_PREVIEW|文件与审阅|打开打印预览|scope|不代表已提交打印任务
DOC_INSPECT|文件与审阅|检查文档问题|check_type|检查隐私或辅助功能不等于执行修复或删除
DOC_STATISTICS|文件与审阅|查看字数或可读性统计|metric|查看入口不代表提取到统计值
DOC_PROPERTIES|文件与审阅|查看或修改文档属性|mode,property,value|不含仅查看字数
DOC_COMPAT_CHECK|文件与审阅|检查文档兼容性|format|不等于转换格式
DOC_CONVERT|文件与审阅|转换文档格式|source_format,target_format|当前请求与实际兼容性检查不一致，未验证转换
DOC_TEMPLATE|文件与审阅|查找模板或从模板新建|mode,query,template|查找与创建保留不同 mode；不含向已有演示应用主题
DOC_SAVE|文件与审阅|保存当前文档|target|不含另存为；按钮记录不证明落盘
DOC_SAVE_AS|文件与审阅|另存为或指定格式保存|path,format|不含导入数据；当前来自请求与动作不一致样本
DOC_PROTECT|文件与审阅|设置或移除文档保护|protection_type,enabled|只读与密码作为参数区分；不公开报告密码值
DOC_HISTORY|文件与审阅|查看文档版本历史|provider|不等于恢复版本，可能依赖云服务
TRACK_CHANGES|文件与审阅|设置修订模式|enabled|按钮多次切换不能证明最终开启
TEXT_INSERT|文本与段落|输入正文或单元格文本|target,text|输入搜索框或属性框属于对应操作参数
TEXT_DELETE|文本与段落|删除正文或段落|selection,scope|不含删除表格行
TEXT_FORMAT|文本与段落|设置字符格式或样式|font,size,color,style,case,effect|段落对齐、分页布局另分；保留标题样式与直接格式差异
PARAGRAPH_FORMAT|文本与段落|设置段落格式|alignment,bullet,drop_cap,border|不含整页垂直对齐；自动套用分隔线与绘制形状区分
PAGE_LAYOUT|页面与结构|设置页面布局|columns,vertical_alignment,orientation,size|Word 分栏与 PPT 页面方向保留 app/target 参数
PAGE_DECORATION|页面与结构|设置水印或页面颜色|kind,value|不含图片颜色调整
HEADER_FOOTER|页面与结构|编辑页眉页脚|target,mode,scope|只打开编辑入口不等于内容修改完成
PAGE_NUMBER|页面与结构|插入页码|position,style,total_pages|选择页码样式不证明满足 Page X of Y
COMMENT_EDIT|文件与审阅|新增或删除评论|mode,text|打开窗格、运行文档检查均不等于删除评论
TOC_UPDATE|页面与结构|更新目录|scope|不含新建目录
TABLE_INSERT|表格与数据|插入表格|rows,columns|不含已有表格结构调整
TABLE_STRUCTURE|表格与数据|增删表格行列|axis,mode,position|不含删除普通正文或单元格内容
TABLE_FORMAT|表格与数据|设置表格样式或边框|style,border|不含字符格式；最终样式状态未经视觉核验
DATA_SORT|表格与数据|排序文本或表格数据|target,key,order|Word 段落和 Excel 区域保留不同 target
FORM_CONTROL|对象与媒体|插入或使用表单控件|control_type,mode,value|使用已有下拉框与插入复选框保留不同 mode
EQUATION|对象与媒体|插入公式或公式结构|structure|不含 Excel 单元格公式填充，当前无此证据
SYMBOL|对象与媒体|插入符号|symbol|普通文本输入不自动归为符号插入
QUICK_PART|对象与媒体|插入自动图文集或文档部件|part|不含普通文本输入
TEXTBOX|对象与媒体|插入文本框|type,position|不等于已经填充文本内容
WORDART|对象与媒体|插入艺术字|style,text|不含普通文字格式调整
SHAPE_INSERT|对象与媒体|插入或绘制形状|shape,position,size|直线和矩形参数化；不含图片插入
SHAPE_FORMAT|对象与媒体|设置形状线条格式|line_style|不含形状插入和图片颜色
SMARTART|对象与媒体|插入 SmartArt 图示|layout|块列表、流程、时间线保留原布局，不认定语义相同
IMAGE_INSERT|对象与媒体|插入图片|source,query|当前样本为在线图片；不访问或下载链接
OBJECT_TRANSFORM|对象与媒体|调整对象大小、旋转或纵横比|target,transform,value|图片与形状保留目标参数；纯坐标拖动只提供弱语义证据
IMAGE_APPEARANCE|对象与媒体|调整图片颜色效果|effect|不含页面或形状填充
OBJECT_ALT_TEXT|对象与媒体|编辑对象替代文字|mode,text|只使用已保留 action 文本，不读取 accessibility tree
MODEL_3D|对象与媒体|插入三维模型|model,source|候选 WPS 支持性未验证，可能依赖在线素材
NAMED_RANGE|表格与数据|定义或管理命名区域|mode,name,range|打开名称管理器不等于创建区域
DATA_QUERY|表格与数据|打开或创建数据查询|mode,source|未观察到完整查询转换流程；WPS 支持性未验证
DATA_IMPORT|表格与数据|导入外部数据|format,source|当前只有 CSV 导入请求，实际记录另存为
DATA_DEDUP|表格与数据|删除重复数据|range,columns|不含普通排序；未核验去重结果
CHART_INSERT|表格与数据|插入图表|range,chart_type|不含 SmartArt 图示
HYPERLINK|对象与媒体|插入超链接|target,address|仅分析文本，不跟随 URL
TRANSLATE|文件与审阅|翻译选中文字或单元格|source_language,target_language|打开翻译窗格与写回译文需区分；可能依赖网络
SPELLCHECK|文件与审阅|运行拼写检查|language|不代表已纠正全部错误
READ_ALOUD|文件与审阅|启动朗读|scope|不下载音频，不验证实际播放
MACRO_RECORD|自动化与演示|开始录制宏|name,scope|目前只有入口证据，未观察录制确认
SLIDE_REORDER|自动化与演示|调整幻灯片顺序|source,destination|纯拖动记录不能独立证明源页与目标页
SLIDE_SECTION|自动化与演示|新增幻灯片节|name|不含新建幻灯片
SLIDE_INSERT|自动化与演示|新建幻灯片|layout|不含新建演示文件
SLIDE_THEME|自动化与演示|应用演示主题|theme|不含从模板创建新文件或界面深浅色主题
SLIDESHOW_SETTINGS|自动化与演示|设置放映选项|mode,range|目前只打开设置窗口，不等于启动放映
SLIDE_TIMING|自动化与演示|设置自动换片时间|seconds|不等于持续执行放映
ANIMATION_REORDER|自动化与演示|调整动画顺序|effect,direction|多次上下移动不代表最终顺序改变
FIND_REPLACE|文本与段落|查找并替换文本|query,replacement,scope|不含 UI 命令搜索
SLIDE_BACKGROUND|自动化与演示|设置幻灯片图片背景|source|当前只有请求，无实际 action
'''

# execution suffix, primary requested candidate, evidence tier, anchor step IDs.
# Reviewed against all local request/function/control_text/args records, not keywords.
PRIMARY = '''
word_1_103|PRINT_PREVIEW|A|2,5
word_1_113|VIEW_LAYOUT|A|8,9
word_1_115|UI_CUSTOMIZE|A|5,6
word_1_119|PAGE_NUMBER|A|9,13
word_1_123|DATA_SORT|A|5,6
word_1_128|EQUATION|A|4
word_1_13|TEXT_DELETE|A|3
word_1_132|TABLE_FORMAT|A|2,4,5,18,19,20,21,22,23
word_1_14|DESIGN_MODE|A|2,5
word_1_145|TRACK_CHANGES|C|3,5,6
word_1_155|QUICK_PART|A|4
word_1_174|TABLE_FORMAT|A|6,7
word_1_18|SHAPE_INSERT|A|3,4,9,10
word_1_181|PARAGRAPH_FORMAT|A|3
word_1_182|PAGE_LAYOUT|A|5,6
word_1_183|TEXT_FORMAT|A|3
word_1_184|TEXT_FORMAT|A|1,2,6
word_1_187|TEXT_FORMAT|A|2
word_1_188|TEXT_FORMAT|A|2,3,4,5
word_1_201|TEXT_FORMAT|A|4,7
word_1_208|DOC_STATISTICS|A|2,3
word_1_212|TABLE_STRUCTURE|A|1
word_1_222|COMMENT_EDIT|A|3,4
word_1_225|PARAGRAPH_FORMAT|A|2
word_1_228|SMARTART|A|4,5
word_1_237|PAGE_LAYOUT|A|4
word_1_4|UI_CUSTOMIZE|A|2,3
word_1_48|FORM_CONTROL|A|3
word_1_49|FORM_CONTROL|A|2,6
word_1_51|EQUATION|A|2
word_1_54|SYMBOL|A|3,7
word_1_56|TABLE_INSERT|A|3,7
word_1_57|TEXTBOX|A|4
word_1_64|WORDART|A|3,4,8,9
word_1_67|DOC_INSPECT|A|5
word_1_77|TOC_UPDATE|A|3,4
word_1_81|READ_ALOUD|A|2,5
word_1_84|HEADER_FOOTER|A|2
word_1_91|DOC_PROTECT|A|5,6
word_2_10|DOC_CONVERT|C|4,5
word_2_125|UI_THEME|I|
word_2_134|VIEW_LAYOUT|A|1
word_2_164|TEXT_INSERT|A|1,5
word_2_176|EQUATION|A|2,6
word_2_179|SYMBOL|A|3,7
word_2_182|PARAGRAPH_FORMAT|A|1,3
word_2_193|SHAPE_INSERT|A|6,7
word_2_199|PAGE_DECORATION|A|3,7
word_2_210|DOC_STATISTICS|I|
word_2_23|PARAGRAPH_FORMAT|A|4
word_2_28|TABLE_INSERT|A|3,8
word_2_29|TABLE_INSERT|A|3
word_2_31|TEXT_INSERT|A|1,4
word_2_33|TEXTBOX|A|3,4
word_2_38|TEXT_FORMAT|A|2
word_2_51|TABLE_INSERT|A|3
word_2_76|TABLE_STRUCTURE|A|4,5
word_2_81|PAGE_DECORATION|A|3,8
word_2_82|PAGE_DECORATION|A|4
word_2_85|PAGE_DECORATION|A|3
word_2_9|DOC_TEMPLATE|A|3,4,11,12
word_2_93|PAGE_LAYOUT|A|4
word_2_98|TEXT_FORMAT|A|3,4
excel_1_105|DOC_PROTECT|A|4
excel_1_110|VIEW_PANE|A|1
excel_1_112|VIEW_LAYOUT|A|2
excel_1_114|UI_CUSTOMIZE|A|4,5
excel_1_118|DATA_SORT|A|2,3
excel_1_122|DATA_QUERY|E|2
excel_1_124|TRANSLATE|A|3,10,11
excel_1_16|SPELLCHECK|A|2,3
excel_1_17|DOC_INSPECT|A|2
excel_1_29|CHART_INSERT|A|2,3
excel_1_32|NAMED_RANGE|A|4,5
excel_1_40|HEADER_FOOTER|E|3
excel_1_42|DATA_QUERY|A|6,9
excel_1_44|NAMED_RANGE|E|3
excel_1_60|SMARTART|A|3,4
excel_1_63|HYPERLINK|A|2,3
excel_1_80|DATA_IMPORT|C|2,3,4,5
excel_1_81|VIEW_PANE|A|25
excel_1_87|DOC_PROTECT|A|5,6,7,8
excel_1_90|MACRO_RECORD|E|3
excel_1_97|DATA_DEDUP|A|3,4
ppt_1_103|COMMENT_EDIT|C|5,6
ppt_1_11|SLIDE_SECTION|A|4,5,6
ppt_1_111|SHAPE_INSERT|A|8,9
ppt_1_112|SHAPE_INSERT|A|10,11
ppt_1_129|FIND_REPLACE|A|4,5,6,7
ppt_1_131|DOC_TEMPLATE|A|3,4
ppt_1_140|DOC_STATISTICS|A|5
ppt_1_150|SHAPE_INSERT|A|3,5
ppt_1_152|MODEL_3D|A|5,6
ppt_1_16|TEXTBOX|A|3
ppt_1_160|SHAPE_INSERT|A|3,5
ppt_1_161|SMARTART|A|5,6
ppt_1_174|OBJECT_TRANSFORM|A|6
ppt_1_193|PAGE_LAYOUT|A|5,6,7
ppt_1_197|SLIDE_REORDER|A|1
ppt_1_206|HEADER_FOOTER|A|3
ppt_1_214|OBJECT_TRANSFORM|A|1
ppt_1_216|OBJECT_TRANSFORM|A|9
ppt_1_223|IMAGE_INSERT|A|5,7,8
ppt_1_226|DOC_HISTORY|E|3,7
ppt_1_230|SLIDE_BACKGROUND|I|
ppt_1_237|SLIDESHOW_SETTINGS|E|2
ppt_1_243|VIEW_GUIDES|A|3,4,5
ppt_1_244|SLIDE_TIMING|A|2,3
ppt_1_256|OBJECT_ALT_TEXT|A|4
ppt_1_257|UI_THEME|E|2
ppt_1_258|VIEW_GUIDES|A|2
ppt_1_259|VIEW_GUIDES|A|2
ppt_1_26|UI_CUSTOMIZE|A|10,11
ppt_1_273|DOC_PROPERTIES|A|5,6
ppt_1_278|SHAPE_INSERT|A|3,4
ppt_1_35|SLIDE_THEME|A|2
ppt_1_54|IMAGE_APPEARANCE|A|9
ppt_1_58|TEXT_FORMAT|A|7,8
ppt_1_59|ANIMATION_REORDER|A|4,5,6,7,8,9
ppt_1_7|SHAPE_INSERT|A|3,4
ppt_1_91|SMARTART|A|7,8
'''

# Additional observed operations, including explicit conflict alternatives.
SECONDARY = {
    'word_2_10': [('DOC_COMPAT_CHECK', [4, 5])],
    'excel_1_80': [('DOC_SAVE_AS', [2, 3, 4, 5])],
    'ppt_1_103': [('DOC_INSPECT', [5, 6]), ('DOC_SAVE', [1])],
    'word_2_164': [('PARAGRAPH_FORMAT', [2, 6])],
    'word_2_9': [('TEXT_INSERT', [5, 13]), ('DOC_SAVE', [6, 14])],
    'word_1_84': [('DOC_SAVE', [4])],
    'ppt_1_111': [('SHAPE_FORMAT', [12, 13]), ('DOC_SAVE', [1, 4, 14])],
    'ppt_1_244': [('DOC_SAVE', [4])],
    'ppt_1_91': [('SLIDE_INSERT', [3])],
    'excel_1_124': [('TEXT_INSERT', [11])],
}

NOTES = {
    'word_1_119': '已选页码样式，但无法证明包含总页数 Page X of Y。',
    'word_1_132': '多次样式点击及窗口切换；含坐标为 null 的点击，不能推断最终样式。',
    'word_1_145': '请求开启修订，动作中出现 Track Changes Off；最终状态存疑。',
    'word_1_184': '部分格式点击在文本选择之前，不能保证均作用于请求文本。',
    'word_1_4': '多次工具栏菜单和打印入口操作，不能确认最终工具栏配置。',
    'word_1_84': '有清空页眉输入，未见同等明确的清空页脚证据。',
    'word_2_10': '请求在必要时转换 DOCX，实际操作为兼容性检查；无法确认是否需要转换或已转换。',
    'word_2_125': '只有结束记录；请求称界面已为浅色，无实际设置动作。',
    'word_2_210': '只有结束记录，无查看字数的 action。',
    'excel_1_112': '请求显示标尺，仅记录切换页面布局，未记录标尺开关。',
    'excel_1_32': '列标题点击不足以独立确认选择范围确为 A1:G1。',
    'excel_1_60': '请求流程图，实际选择 Basic Block List，布局存在差异。',
    'excel_1_80': '请求导入 CSV，实际记录另存为 Excel Workbook，不能计为数据导入。',
    'excel_1_81': '26 条记录中包含大量滚轮及重复入口点击，不能当作 26 次导航操作。',
    'ppt_1_103': '请求删除评论，只见文档检查与 Inspect，无 Remove All 或删除确认。',
    'ppt_1_197': '只有坐标拖动；移页语义依赖请求，源页和目标页无法独立确认。',
    'ppt_1_214': '只有坐标拖动；调整图片尺寸的语义依赖请求，尺寸变化未验证。',
    'ppt_1_206': '勾选 Footers 的终态未保留，不能独立确认已删除全部页脚。',
    'ppt_1_230': '只有结束记录，无图片背景设置动作。',
    'ppt_1_257': '只进入 File / Account，未记录 Black 或深色主题选择。',
    'ppt_1_54': '先多次进入 Shape Format，随后才记录图片颜色 Sepia。',
    'ppt_1_59': '动画上下移动交替，不能断言最终顺序已发生变化。',
    'ppt_1_131': '在线模板搜索；仅文本分析，不访问模板资源。',
    'ppt_1_223': 'Bing 在线图片搜索与插入；不访问图片链接。',
    'ppt_1_226': '版本历史涉及 OneDrive，只有入口记录，未恢复版本。',
    'ppt_1_152': '模型图库选择；WPS 兼容性与资源依赖未验证。',
    'excel_1_124': '翻译入口和单元格写入均有记录，未核验译文正确性。',
}


def catalog():
    result = {}
    for line in CATALOG.strip().splitlines():
        code, family, name, parameters, boundary = line.split('|')
        if code in result:
            raise ValueError('Duplicate candidate: ' + code)
        result[code] = dict(operation_id='WPSV1.' + code, family=family,
                            name=name, parameters=parameters.split(','), boundary=boundary,
                            wps_compatibility='unverified', completion_verified=False)
    return result


def annotations():
    result = {}
    for line in PRIMARY.strip().splitlines():
        execution_id, code, tier, steps = line.split('|')
        if execution_id in result:
            raise ValueError('Duplicate annotation: ' + execution_id)
        result[execution_id] = dict(candidate=code, tier=tier,
                                    anchor_steps=[int(s) for s in steps.split(',') if s],
                                    note=NOTES.get(execution_id, ''))
    return result
