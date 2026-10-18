module demo(input s, t, a, b, c, d, input [3:0] bus,
            output reg y, overwritten, output reg [3:0] partial);
    always @* begin
        if (s) begin
            if (t)
                y = a;
            else
                y = b;
        end else
            y = c;
    end
    always @* begin
        overwritten = a & b;
        overwritten = c ^ d;
    end
    always @* begin
        partial = bus;
        partial[1:0] = 2'b01;
    end
endmodule
