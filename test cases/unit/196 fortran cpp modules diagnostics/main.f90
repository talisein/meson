program main
  use, intrinsic :: iso_c_binding, only: c_int
  implicit none
  interface
    function fortran_square(x) bind(C, name="fortran_square")
      import :: c_int
      integer(c_int), value :: x
      integer(c_int) :: fortran_square
    end function fortran_square
  end interface

  ! Actually call into the modules-using C++ library.
  if (fortran_square(5_c_int) /= 25) then
    error stop 1
  end if
end program main
