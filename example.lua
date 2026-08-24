-- luaRE crackme sample
-- The expected serial is never stored as a plaintext string.  Validation is
-- split across an indirect state machine, byte permutation, rolling transform,
-- and independent arithmetic guards so that strings alone are insufficient.

local floor = math.floor
local byte = string.byte

local function bxor(left, right)
    local result, place = 0, 1
    for _ = 1, 8 do
        local a, b = left % 2, right % 2
        if a ~= b then
            result = result + place
        end
        left = floor(left / 2)
        right = floor(right / 2)
        place = place * 2
    end
    return result
end

local function rol8(value, count)
    count = count % 8
    if count == 0 then
        return value % 256
    end
    local upper = (value * (2 ^ count)) % 256
    local lower = floor(value / (2 ^ (8 - count)))
    return upper + lower
end

local function bytes_of(text)
    local result = {}
    for index = 1, #text do
        result[index] = byte(text, index)
    end
    return result
end

local function shuffled_indices(length, seed)
    local order = {}
    for index = 1, length do
        order[index] = index
    end
    for index = length, 2, -1 do
        seed = (seed * 109 + 89) % 997
        local other = (seed % index) + 1
        order[index], order[other] = order[other], order[index]
    end
    return order
end

local function key_stream(initial)
    local current = initial
    return function()
        current = (current * 73 + 41) % 256
        return current
    end
end

local function transform(values)
    local order = shuffled_indices(#values, 431)
    local next_key = key_stream(0x6D)
    local output, state = {}, 0x5B

    for logical = 1, #order do
        local physical = order[logical]
        local value = values[physical]
        local key = next_key()
        state = (state + bxor(value, key) + logical * 17) % 256
        local rotated = rol8((value + state) % 256, ((logical + key) % 7) + 1)
        output[logical] = bxor(rotated, (key + physical * 13) % 256)
    end
    return output, state
end

local function folded_signature(values)
    local first, second = 0x31, 0xA7
    for index, value in ipairs(values) do
        first = (first + value * index + rol8(value, (index % 7) + 1)) % 257
        second = (second + bxor(value, first % 256) + index * 23) % 263
    end
    return first, second
end

local function equal_scrambled(actual, expected)
    if #actual ~= #expected then
        return false
    end
    local order = shuffled_indices(#expected, 719)
    local mismatch = 0
    for _, index in ipairs(order) do
        mismatch = mismatch + bxor(actual[index], expected[index])
    end
    return mismatch == 0
end

local function pair_guards(values)
    local expected = { 96, 210, 87, 151, 10, 68, 0, 158 }
    local mismatch = 0
    for index = 1, 8 do
        local combined = (values[index] + values[17 - index] * (index * 2 + 3)) % 257
        mismatch = mismatch + bxor(combined, expected[index])
    end
    return mismatch == 0
end

local function debugger_probe()
    if type(debug) ~= "table" then
        return true
    end
    local visible_hook = debug.gethook and debug.gethook()
    if visible_hook ~= nil then
        return false
    end
    -- A basic anti-debug routine often attempts this. luaRE live mode protects
    -- its private hook while presenting the expected public behavior.
    if debug.sethook then
        pcall(debug.sethook)
    end
    return true
end

local function opaque_tag(value)
    return (value * 37 + 19) % 251
end

local dispatch = {}

dispatch[opaque_tag(7)] = function(context)
    if #context.input ~= 16 then
        context.reason = 1
        return opaque_tag(91)
    end
    context.values = bytes_of(context.input)
    return opaque_tag(23)
end

dispatch[opaque_tag(23)] = function(context)
    if not debugger_probe() then
        context.reason = 2
        return opaque_tag(91)
    end
    context.transformed, context.tail = transform(context.values)
    return opaque_tag((context.tail + 41) % 97)
end

dispatch[opaque_tag(45)] = function(context)
    local expected = {
        87, 48, 224, 89, 67, 201, 244, 172,
        140, 59, 76, 55, 213, 56, 175, 226,
    }
    if context.tail ~= 101 or not equal_scrambled(context.transformed, expected) then
        context.reason = 3
        return opaque_tag(91)
    end
    return opaque_tag(61)
end

dispatch[opaque_tag(61)] = function(context)
    local first, second = folded_signature(context.values)
    if first ~= 144 or second ~= 254 then
        context.reason = 4
        return opaque_tag(91)
    end
    return opaque_tag(74)
end

dispatch[opaque_tag(74)] = function(context)
    if not pair_guards(context.values) then
        context.reason = 5
        return opaque_tag(91)
    end
    context.accepted = true
    return opaque_tag(88)
end

dispatch[opaque_tag(91)] = function(context)
    -- Keep failure handling in the same dispatcher to make the terminal edge
    -- less obvious in a source-order reading.
    context.accepted = false
    return opaque_tag(88)
end

-- Unreachable decoy handlers deliberately resemble real validation stages.
dispatch[opaque_tag(12)] = function(context)
    context.tail = bxor(context.tail or 0, 0xA6)
    return opaque_tag(61)
end

dispatch[opaque_tag(39)] = function(context)
    context.reason = ((context.reason or 0) * 11 + 7) % 13
    return opaque_tag(12)
end

local function verify(candidate)
    local context = { input = candidate or "", accepted = false, steps = 0 }
    local state = opaque_tag(7)
    local terminal = opaque_tag(88)

    while state ~= terminal do
        context.steps = context.steps + 1
        if context.steps > 12 then
            return false
        end
        local handler = dispatch[state]
        if not handler then
            return false
        end
        state = handler(context)
    end
    return context.accepted
end

local function login(candidate)
    if verify(candidate) then
        print("access granted")
        return true
    end
    print("access denied")
    return false
end

io.write("input serial: ")
io.flush()
login(io.read("l"))
