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
    return {'chatters': int(chatters.user.nunique()), 'gifters': int(supporters.user.nunique()),
        'cross_room_engagers': int((room_counts >= 2).sum()), 'top_share': top_share,
        'daily': pd.DataFrame(daily)}
