-- luaRE runtime trace bootstrap. Events are prefixed so target stdout remains usable.
local target = assert(arg[1], "target Lua file is required")
local max_events = tonumber(arg[2]) or 20000
local event_prefix = "@@LUARE@@"
local target_source = "@" .. target
local event_count = 0
local call_depth = 0
local live_mode = arg[3] == "--luare-live"
local target_arg_start = live_mode and 4 or 3
local pause_mode = live_mode and "step" or "continue"
local pause_depth = 0
local breakpoints = {}
local temporary_breakpoints = {}
local public_debug = debug
local real_getinfo = debug.getinfo
local real_getlocal = debug.getlocal
local real_setlocal = debug.setlocal
local real_getupvalue = debug.getupvalue
local real_setupvalue = debug.setupvalue
local real_sethook = debug.sethook
local real_traceback = debug.traceback
local real_load = load
local real_loadstring = loadstring
local real_setfenv = setfenv
local real_pcall = pcall
local real_io_read = io.read

local function json_string(value)
    return '"' .. value:gsub('[%z\1-\31\\"]', function(ch)
        local escapes = { ['"']='\\"', ['\\']='\\\\', ['\b']='\\b', ['\f']='\\f', ['\n']='\\n', ['\r']='\\r', ['\t']='\\t' }
        return escapes[ch] or string.format('\\u%04X', string.byte(ch))
    end) .. '"'
end

local function is_array(value)
    local count, highest = 0, 0
    for key in next, value do
        if type(key) ~= "number" or key < 1 or key % 1 ~= 0 then return false end
        count = count + 1
        if key > highest then highest = key end
    end
    return highest == count
end

local function json(value)
    local kind = type(value)
    if kind == "nil" then return "null" end
    if kind == "boolean" then return value and "true" or "false" end
    if kind == "number" then
        if value ~= value or value == math.huge or value == -math.huge then return json_string(tostring(value)) end
        return tostring(value)
    end
    if kind == "string" then return json_string(value) end
    if kind ~= "table" then return json_string("<" .. kind .. ">") end
    local parts = {}
    if is_array(value) then
        for i = 1, #value do parts[#parts + 1] = json(value[i]) end
        return "[" .. table.concat(parts, ",") .. "]"
    end
    for key, item in next, value do
        parts[#parts + 1] = json_string(tostring(key)) .. ":" .. json(item)
    end
    return "{" .. table.concat(parts, ",") .. "}"
end

local function snapshot(value, depth, seen)
    local kind = type(value)
    if kind == "nil" or kind == "boolean" or kind == "number" then
        return { type=kind, value=value }
    end
    if kind == "string" then
        local clipped = #value > 512
        return { type=kind, value=clipped and value:sub(1, 512) or value, clipped=clipped or nil }
    end
    if kind == "function" then
        local ok, info = pcall(real_getinfo, value, "nS")
        return { type=kind, name=ok and info and info.name or nil, source=ok and info and info.short_src or nil, line=ok and info and info.linedefined or nil }
    end
    if kind == "thread" then return { type=kind, status=coroutine.status(value) } end
    if kind ~= "table" then return { type=kind } end
    if depth >= 1 or seen[value] then return { type="table", recursive=seen[value] or nil } end
    seen[value] = true
    local entries, key, count = {}, nil, 0
    while count < 12 do
        key = next(value, key)
        if key == nil then break end
        count = count + 1
        entries[#entries + 1] = { key=snapshot(key, depth + 1, seen), value=snapshot(value[key], depth + 1, seen) }
    end
    seen[value] = nil
    return { type="table", entries=entries, truncated=key ~= nil or nil }
end

local function locals_at(level)
    local values, index = {}, 1
    while true do
        local name, value = real_getlocal(level, index)
        if not name then break end
        if name:sub(1, 1) ~= "(" then
            values[#values + 1] = { name=name, index=index, value=snapshot(value, 0, {}) }
        end
        index = index + 1
    end
    return values
end

local function upvalues_at(level)
    local values = {}
    local info = real_getinfo(level, "f")
    if not info or not info.func then return values end
    local index = 1
    while true do
        local name, value = real_getupvalue(info.func, index)
        if not name then break end
        if name ~= "_ENV" then
            values[#values + 1] = { name=name, index=index, value=snapshot(value, 0, {}) }
        end
        index = index + 1
    end
    return values
end

local function stack_at(level)
    local frames = {}
    while #frames < 32 do
        local info = real_getinfo(level, "nSl")
        if not info then break end
        if info.source == target_source then
            frames[#frames + 1] = { name=info.name or "<chunk>", line=info.currentline, defined=info.linedefined }
        end
        level = level + 1
    end
    return frames
end

local function emit(event)
    event_count = event_count + 1
    event.seq = event_count
    io.stdout:write(event_prefix, json(event), "\n")
    io.stdout:flush()
end

local function should_pause(line)
    if not live_mode then return false end
    if temporary_breakpoints[line] then
        temporary_breakpoints[line] = nil
        return true
    end
    if breakpoints[line] then return true end
    if pause_mode == "step" then return true end
    if pause_mode == "over" and call_depth <= pause_depth then return true end
    if pause_mode == "out" and call_depth < pause_depth then return true end
    return false
end

local function decode_hex(encoded)
    if #encoded % 2 ~= 0 or encoded:find("[^%x]") then return nil, "invalid hex payload" end
    return (encoded:gsub("..", function(pair) return string.char(tonumber(pair, 16)) end))
end

local function evaluate_value(encoded)
    local expression, decode_error = decode_hex(encoded)
    if not expression then return false, decode_error end
    local chunk, compile_error
    if _VERSION == "Lua 5.1" and real_loadstring then
        chunk, compile_error = real_loadstring("return " .. expression, "=(luaRE value)")
        if chunk and real_setfenv then real_setfenv(chunk, {}) end
    else
        chunk, compile_error = real_load("return " .. expression, "=(luaRE value)", "t", {})
    end
    if not chunk then return false, compile_error end
    local ok, value = real_pcall(chunk)
    if not ok then return false, value end
    return true, value
end

local function set_paused_value(scope, index, encoded)
    local ok, value = evaluate_value(encoded)
    if not ok then return false, value end
    -- set_paused_value -> await_command -> hook -> target frame
    if scope == "local" then
        local name = real_setlocal(4, index, value)
        if not name then return false, "local is no longer available" end
        return true, name
    end
    local info = real_getinfo(4, "f")
    if not info or not info.func then return false, "function frame is no longer available" end
    local name = real_setupvalue(info.func, index, value)
    if not name then return false, "upvalue is no longer available" end
    return true, name
end

local function target_io_read(...)
    local formats = {...}
    emit({ event="input_request", formats=formats, depth=call_depth })
    while true do
        local command = real_io_read("*l")
        if not command then return nil end
        if command == "quit" then
            real_sethook()
            error("__LUARE_QUIT__", 0)
        end
        local encoded = command:match("^input%s+([%x]+)$")
        if command == "input -" then encoded = "" end
        if encoded ~= nil then
            local value, decode_error = decode_hex(encoded)
            if not value then
                emit({ event="command_error", message=decode_error, depth=call_depth })
            else
                local format = formats[1]
                if format == "n" or format == "*n" then return tonumber(value) end
                if format == "L" or format == "*L" then return value .. "\n" end
                return value
            end
        else
            emit({ event="command_error", message="target is waiting for input", depth=call_depth })
        end
    end
end

local function await_command(paused_item)
    while true do
        local command = real_io_read("*l")
        if not command then error("__LUARE_QUIT__", 0) end
        if command == "step" then
            pause_mode = "step"
            return
        elseif command == "over" then
            pause_mode = "over"
            pause_depth = call_depth
            return
        elseif command == "out" then
            pause_mode = "out"
            pause_depth = call_depth
            return
        elseif command == "continue" then
            pause_mode = "continue"
            return
        elseif command == "quit" then
            real_sethook()
            error("__LUARE_QUIT__", 0)
        else
            local break_line = tonumber(command:match("^break%s+(%d+)$"))
            local clear_line = tonumber(command:match("^clear%s+(%d+)$"))
            local runto_line = tonumber(command:match("^runto%s+(%d+)$"))
            local value_scope, value_index, value_payload
            value_index, value_payload = command:match("^setlocal%s+(%d+)%s+lua%s+(%x+)$")
            if value_index then
                value_scope = "local"
            else
                value_index, value_payload = command:match("^setupvalue%s+(%d+)%s+lua%s+(%x+)$")
                if value_index then value_scope = "upvalue" end
            end
            if break_line then
                breakpoints[break_line] = true
                emit({ event="breakpoint_set", line=break_line, depth=call_depth })
            elseif clear_line then
                breakpoints[clear_line] = nil
                emit({ event="breakpoint_cleared", line=clear_line, depth=call_depth })
            elseif runto_line then
                temporary_breakpoints[runto_line] = true
                pause_mode = "continue"
                return
            elseif value_scope then
                local ok, name_or_error = set_paused_value(value_scope, tonumber(value_index), value_payload)
                if ok then
                    emit({
                        event="value_set", paused=true, scope=value_scope, name=name_or_error,
                        line=paused_item.line, function_name=paused_item.function_name, depth=call_depth,
                        locals=locals_at(4), upvalues=upvalues_at(4), stack=stack_at(4),
                    })
                else
                    emit({ event="command_error", paused=true, message=name_or_error, line=paused_item.line, depth=call_depth })
                end
            else
                emit({ event="command_error", message="unknown command: " .. command, depth=call_depth })
            end
        end
    end
end

local function hook(event, line)
    local info = real_getinfo(2, "nSfl")
    if not info or info.source ~= target_source then return end
    if event == "call" then call_depth = call_depth + 1 end
    local item = {
        event=event == "tail call" and "tail_call" or event,
        line=line or info.currentline,
        function_name=info.name or (info.linedefined == 0 and "<chunk>" or "<anonymous>"),
        defined_line=info.linedefined,
        depth=call_depth,
    }
    if event == "line" then
        item.locals = locals_at(3)
        item.upvalues = upvalues_at(3)
        item.stack = stack_at(3)
        item.paused = should_pause(line or info.currentline) or nil
    end
    emit(item)
    if event_count >= max_events then
        real_sethook()
        error("luaRE trace event limit reached", 0)
    end
    if item.paused then await_command(item) end
    if event == "return" or event == "tail return" then call_depth = math.max(0, call_depth - 1) end
end

local chunk, load_error = loadfile(target)
if not chunk then
    emit({ event="error", message=load_error, depth=0 })
    os.exit(1)
end

local original_arg = arg
local script_arg = { [0]=target }
for index = target_arg_start, #original_arg do script_arg[index - target_arg_start + 1] = original_arg[index] end
_G.arg = script_arg
real_sethook(hook, "crl")
if live_mode then
    -- Keep the controller hook private from ordinary Lua-level anti-debug checks.
    public_debug.gethook = function() return nil end
    public_debug.sethook = function() return nil end
    io.read = target_io_read
end
emit({ event="session_start", target=target, depth=0 })
local ok, runtime_error = xpcall(chunk, real_traceback)
real_sethook()
if ok then
    emit({ event="session_end", depth=0 })
elseif runtime_error:find("__LUARE_QUIT__", 1, true) then
    emit({ event="session_end", reason="quit", depth=call_depth })
    ok = true
else
    emit({ event="error", message=runtime_error, depth=call_depth })
end
if not ok then os.exit(1) end
