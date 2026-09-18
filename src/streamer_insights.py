"""Evidence-bound creator insights from already deduplicated captured events."""
import pandas as pd

def creator_summary(events):
    known = events[events.user != 'unknown']
    chatters = known[known.chat == 1]
    supporters = known[known.gifts == 1]
    engaged = known[(known.chat == 1) | (known.gifts == 1)]
    room_counts = engaged[engaged.room_id != 'unknown'].groupby('user').room_id.nunique()
    gift_totals = supporters.groupby('user').diamonds.sum().sort_values(ascending=False)
    total = float(events.diamonds.sum())
    top_share = float(gift_totals.iloc[0]) / total * 100 if total > 0 and not gift_totals.empty else None
    daily = []
    daily_events = events[~events.type.isin(['system', 'unknown'])] if 'type' in events else events
    for day, group in daily_events.groupby('date'):
        users = group[(group.user != 'unknown') & (group.chat == 1)].user.nunique()
        gifters = group[(group.user != 'unknown') & (group.gifts == 1)].user.nunique()
        daily.append({'date': day, 'chatters': users, 'gifters': gifters, 'shares': int(group.shares.sum()),
            'follows': int(group.follows.sum()), 'subscribes': int(group.subscribes.sum())})
    declines = []
    for room, group in events[(events.room_id != 'unknown') & events.viewer_count.notna()].groupby('room_id'):
        group = group.sort_values('time')
        if len(group) < 20:
            continue
        n = max(1, len(group) // 3)
        early = float(group.head(n).viewer_count.mean())
        late = float(group.tail(n).viewer_count.mean())
        if early > 0 and late < early * 0.8:
            declines.append({'room': room, 'early': early, 'late': late, 'drop': (1-late/early)*100})
    actions = []
    if top_share is not None and top_share >= 50:
        actions.append(('送禮集中', f'最高送禮者占捕獲 Diamonds 的 {top_share:.1f}%（提醒門檻為 50%）。',
            '下一場測試更廣泛的互動邀請與感謝方式，觀察送禮者人數是否增加。', '漏收 Gift 可能改變占比；不是官方收益或觀眾支持度評分。'))
    if declines:
        item = max(declines, key=lambda x: x['drop'])
        actions.append(('收集後段人流較低', f"房間 {item['room']}：前段平均 {item['early']:.1f} 人，後段 {item['late']:.1f} 人，下降 {item['drop']:.1f}%（門檻 20%）。",
            '在下一場相近階段加入一次提問或內容切換，並記下時間，再比較人數與留言變化。',
            '前後段依收到的樣本分成三份；收集缺口、開播時段與自然下播都可能影響結果，不能視為留存率。'))
    actions.append(('互動與內容測試', f'捕獲到 {chatters.user.nunique()} 位可辨識留言者，{int(events.shares.sum())} 次分享事件。',
        '下一場每隔一段時間做一次明確提問，記錄時間，比較前後的留言者與分享；一次只改一項。',
        '缺少官方不重複觀眾與內容標記，不能判定互動率偏低或哪段內容造成變化。'))
    return {'chatters': int(chatters.user.nunique()), 'gifters': int(supporters.user.nunique()),
        'cross_room_engagers': int((room_counts >= 2).sum()), 'top_share': top_share,
        'daily': pd.DataFrame(daily), 'actions': actions}
