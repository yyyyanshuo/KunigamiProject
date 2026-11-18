(function (global) {
  'use strict';
  const aliases = {"图片": "IMAGE", "画像": "IMAGE", "Image": "IMAGE", "表情": "STICKER", "絵文字": "STICKER", "Sticker": "STICKER", "转账": "TRANSFER", "领取转账": "TRANSFER_ACCEPT", "退回转账": "TRANSFER_RETURN", "已领取转账": "TRANSFER_ACCEPTED", "已退回转账": "TRANSFER_RETURNED", "视频通话": "VIDEO_CALL", "ビデオ通話": "VIDEO_CALL", "Video Call": "VIDEO_CALL", "文件": "FILE", "ファイル": "FILE", "File": "FILE", "音乐": "MUSIC", "音楽": "MUSIC", "Music": "MUSIC", "链接": "LINK", "リンク": "LINK", "Link": "LINK", "视频": "VIDEO", "ビデオ": "VIDEO", "Video": "VIDEO", "已读不回": "READ_RECEIPT", "既読スルー": "READ_RECEIPT", "Read Receipt": "READ_RECEIPT", "未回复": "UNANSWERED", "未返信": "UNANSWERED", "Unanswered": "UNANSWERED"};
  const legacy = {"IMAGE": "图片", "STICKER": "表情", "TRANSFER": "转账", "TRANSFER_ACCEPT": "领取转账", "TRANSFER_RETURN": "退回转账", "TRANSFER_ACCEPTED": "已领取转账", "TRANSFER_RETURNED": "已退回转账", "VIDEO_CALL": "视频通话", "FILE": "文件", "MUSIC": "音乐", "LINK": "链接", "VIDEO": "视频", "READ_RECEIPT": "已读不回", "UNANSWERED": "未回复"};
  function convert(value, mapping) {
    const text = String(value == null ? '' : value);
    const plain = part => part.replace(/\[([^\]:(（：]+)(?=\]|[:：（(])/g, (match, name) => Object.prototype.hasOwnProperty.call(mapping, name) ? '[' + mapping[name] : match);
    const tags = global.KunigamiImageTags ? global.KunigamiImageTags.findAll(text) : [];
    let cursor = 0, output = '';
    tags.forEach(tag => {
      output += plain(text.slice(cursor, tag.start));
      const end = tag.raw.indexOf(']') + 1;
      output += plain(tag.raw.slice(0, end)) + tag.raw.slice(end);
      cursor = tag.end;
    });
    return output + plain(text.slice(cursor));
  }
  global.SakiMessageTags = {
    canonical: value => convert(value, aliases),
    legacy: value => convert(convert(value, aliases), Object.fromEntries(Object.entries(legacy).filter(([name]) => name === "STICKER" || name === "IMAGE" || name.startsWith("TRANSFER"))))
  };
})(window);
