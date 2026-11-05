---
name: xianyu
display: 闲鱼
packages: ["com.taobao.idlefish"]
aliases: ["闲鱼", "Goofish", "xianyu"]
sites: []
open: 闲鱼
pace: careful
description: 二手商品、个人出租转租与私信
---
## 是什么
闲鱼是二手交易 App,包名 `com.taobao.idlefish`。

## 功能
- **商品与房源**:除二手商品外有个人出租、转租。能看到原文描述、挂牌价、押付方式、水电单价与电表、宠物限制。通常需要登录。价格和「房东本人」这类说法都是发布者自己写的。
- **私信卖家**:商品页上的「聊一聊」进入和卖家的会话,可发消息、看回复。消息发出后会话里会出现这一条。
- **私信输入栏**:「想跟TA说点什么...」可能显示为 View。确认是当前会话的空输入栏后,用新鲜的 ref 和 snapshotId 调 `phone_type_text`,也可传该栏内部的坐标。保持 pressEnter 为 false。米卡会走长按粘贴并读回草稿。已有草稿或读回不一致时先停下核对,别重复写入。
