local function foo(x)
    print("foo", x)
    return x * 2
end

function bar()
    print("bar")
end

local x = 4
if x > 3 then foo(x) else bar() end

while x > 0 do
    x = x - 1; foo(x)
end
