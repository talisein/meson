program main
  use, intrinsic :: iso_c_binding, only: c_int
  use fmod, only: add_one
  implicit none
  interface
    function cpp_square(x) bind(C, name="cpp_square")
      import :: c_int
      integer(c_int), value :: x
      integer(c_int) :: cpp_square
    end function cpp_square
  end interface

  ! Exercise both module systems in one target: the C++ named module through
  ! the extern "C" boundary, and the Fortran module through `use`.
  if (cpp_square(5_c_int) /= 25) then
    error stop 1
  end if
  if (add_one(41_c_int) /= 42) then
    error stop 2
  end if
end program main
