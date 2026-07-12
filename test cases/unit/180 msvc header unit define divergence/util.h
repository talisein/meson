#pragma once

#ifdef FOO
inline constexpr bool built_with_foo = true;
#else
inline constexpr bool built_with_foo = false;
#endif
