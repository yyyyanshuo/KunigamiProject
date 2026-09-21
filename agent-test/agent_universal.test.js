const test = require('node:test');
const assert = require('node:assert/strict');

const {
    captureSnapshot,
    chatViaCharApi,
    executeCommands,
    hasWebCommands,
    normalizeClickableText
} = require('./agent_universal');

function makeElement({
    text = '',
    value = '',
    enabled = true,
    visible = true,
    attributes = {},
    tagName = 'BUTTON'
} = {}) {
    return {
        innerText: text,
        value,
        visible,
        disabled: !enabled,
        tagName,
        clicked: 0,
        attributes: { ...attributes },
        getAttribute(name) {
            return Object.prototype.hasOwnProperty.call(this.attributes, name)
                ? this.attributes[name]
                : null;
        },
        setAttribute(name, attributeValue) {
            this.attributes[name] = String(attributeValue);
        }
    };
}

class FakeLocator {
    constructor(elements) {
        this.elements = elements;
    }

    first() {
        return new FakeLocator(this.elements.slice(0, 1));
    }

    nth(index) {
        return new FakeLocator(this.elements.slice(index, index + 1));
    }

    async count() {
        return this.elements.length;
    }

    async isVisible() {
        return Boolean(this.elements[0]?.visible);
    }

    async isEnabled() {
        return Boolean(this.elements[0] && !this.elements[0].disabled);
    }

    async innerText() {
        return this.elements[0]?.innerText || '';
    }

    async getAttribute(name) {
        return this.elements[0]?.getAttribute(name) ?? null;
    }

    async click() {
        const element = this.elements[0];
        if (!element) throw new Error('missing element');
        if (element.disabled) throw new Error('disabled element');
        element.clicked += 1;
    }
}

class FakePage {
    constructor(elements) {
        this.elements = elements;
    }

    locator(selector) {
        const refMatch = selector.match(
            /^\[data-kunigami-agent-ref="(.+)"\]$/
        );
        if (refMatch) {
            return new FakeLocator(
                this.elements.filter(element =>
                    element.getAttribute('data-kunigami-agent-ref') === refMatch[1]
                )
            );
        }
        return new FakeLocator(this.elements);
    }
}

test('normalizes layout whitespace between emoji and text', () => {
    assert.equal(
        normalizeClickableText('🇨🇳\n中国'),
        normalizeClickableText('🇨🇳 中国')
    );
});

test('executes only the first web command and matches split button text', async () => {
    const china = makeElement({ text: '🇨🇳\n中国' });
    const start = makeElement({ text: '开始答题', enabled: false });
    const page = new FakePage([china, start]);

    const result = await executeCommands(
        page,
        '[CLICK:🇨🇳 中国]\n[CLICK:开始答题]'
    );

    assert.deepEqual(result, {
        command: '[CLICK:🇨🇳 中国]',
        success: true,
        message: '操作执行成功'
    });
    assert.equal(china.clicked, 1);
    assert.equal(start.clicked, 0);
});

test('clicks a snapshot element by reference', async () => {
    const china = makeElement({
        text: '🇨🇳\n中国',
        attributes: { 'data-kunigami-agent-ref': 'btn-3' }
    });
    const page = new FakePage([china]);

    const result = await executeCommands(page, '[CLICK_REF:btn-3]');

    assert.equal(result.success, true);
    assert.equal(china.clicked, 1);
    assert.equal(hasWebCommands('[CLICK_REF:btn-3]'), true);
});

test('returns a failure result for a disabled referenced element', async () => {
    const start = makeElement({
        text: '开始答题',
        enabled: false,
        attributes: { 'data-kunigami-agent-ref': 'btn-16' }
    });
    const page = new FakePage([start]);

    const result = await executeCommands(page, '[CLICK_REF:btn-16]');

    assert.equal(result.success, false);
    assert.match(result.message, /不可点击|禁用/);
    assert.equal(start.clicked, 0);
});

test('snapshot exposes stable references and enabled state', async () => {
    const china = makeElement({ text: '🇨🇳\n中国' });
    const start = makeElement({ text: '开始答题', enabled: false });
    const originalDocument = global.document;
    const originalWindow = global.window;

    const page = {
        async waitForLoadState() {},
        async evaluate(callback) {
            global.document = {
                body: { innerText: '中国\n开始答题' },
                querySelectorAll(selector) {
                    if (selector === 'input, textarea') return [];
                    return [china, start];
                }
            };
            global.window = {
                location: { href: 'https://example.test/config' }
            };

            try {
                return callback();
            } finally {
                global.document = originalDocument;
                global.window = originalWindow;
            }
        }
    };

    const snapshot = await captureSnapshot(page);

    assert.match(snapshot.buttons, /ref="btn-0" enabled=true/);
    assert.match(snapshot.buttons, /建议指令:\[CLICK_REF:btn-0\]/);
    assert.match(snapshot.buttons, /ref="btn-1" enabled=false/);
    assert.equal(
        china.getAttribute('data-kunigami-agent-ref'),
        'btn-0'
    );
});

test('includes the previous browser action result in the next AI prompt', async () => {
    const originalFetch = global.fetch;
    let sentMessage = '';

    global.fetch = async (_url, options) => {
        sentMessage = JSON.parse(options.body).message;
        return {
            async json() {
                return { full_reply: '[WAIT]' };
            }
        };
    };

    try {
        await chatViaCharApi(
            {
                url: 'https://example.test',
                text: '测试页面',
                inputs: '',
                buttons: ''
            },
            'sushi',
            2,
            'session=test',
            {
                command: '[CLICK_REF:btn-3]',
                success: false,
                message: '元素编号已失效'
            }
        );
    } finally {
        global.fetch = originalFetch;
    }

    assert.match(sentMessage, /【上一轮浏览器操作结果】/);
    assert.match(sentMessage, /指令：\[CLICK_REF:btn-3\]/);
    assert.match(sentMessage, /状态：失败/);
    assert.match(sentMessage, /每轮只能输出一个网页操作指令/);
});
